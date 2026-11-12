"""inventory: stock levels, cached in Redis, source of truth in Postgres."""

from __future__ import annotations

import os

import psycopg
import redis
from fastapi import HTTPException
from prometheus_client import Counter, Gauge
from psycopg_pool import ConnectionPool

from sut.common import create_app, env_int, log, timed_dependency

app = create_app()

CACHE = Counter("cache_requests_total", "Stock cache lookups", ["result"])
POOL_IN_USE = Gauge("db_pool_connections_in_use", "Connections checked out of the pool")
POOL_WAITING = Gauge("db_pool_requests_waiting", "Requests waiting for a pool connection")
POOL_MAX = Gauge("db_pool_connections_max", "Pool size")
cache = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    socket_connect_timeout=1.0,
    socket_timeout=1.0,
)
pool = ConnectionPool(
    os.environ["DATABASE_URL"],
    min_size=2,
    max_size=env_int("DB_POOL_SIZE", 10),
    timeout=5.0,
    kwargs={"options": f"-c statement_timeout={env_int('DB_STATEMENT_TIMEOUT_MS', 3000)}"},
    open=True,
)

POOL_MAX.set(env_int("DB_POOL_SIZE", 10))


@app.middleware("http")
async def pool_gauges(request, call_next):
    stats = pool.get_stats()
    POOL_IN_USE.set(stats.get("pool_size", 0) - stats.get("pool_available", 0))
    POOL_WAITING.set(stats.get("requests_waiting", 0))
    return await call_next(request)


# Release 1.9.0 added an in-memory audit trail of reservations that is never trimmed.
AUDIT_LEAK_KB = env_int("AUDIT_LEAK_KB", 0)
_audit: list[bytes] = []


def stock_from_db(sku: str) -> int:
    with pool.connection() as conn, timed_dependency("postgres", "select_stock"):
        row = conn.execute(
            "SELECT stock - reserved FROM inventory WHERE sku = %s", (sku,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown sku")
    return int(row[0])


@app.get("/stock/{sku}")
def get_stock(sku: str) -> dict:
    try:
        with timed_dependency("redis", "get"):
            cached = cache.get(f"stock:{sku}")
        if cached is not None:
            CACHE.labels("hit").inc()
            return {"sku": sku, "available": int(cached)}
        CACHE.labels("miss").inc()
    except redis.RedisError as exc:
        CACHE.labels("error").inc()
        log.error(
            f"redis unavailable, falling back to database: {exc}",
            extra={"dependency": "redis", "error": type(exc).__name__},
        )
    available = stock_from_db(sku)
    try:
        with timed_dependency("redis", "set"):
            cache.set(f"stock:{sku}", available, ex=30)
    except redis.RedisError:
        pass
    return {"sku": sku, "available": available}


@app.post("/stock/{sku}/reserve")
def reserve(sku: str, qty: int = 1) -> dict:
    if AUDIT_LEAK_KB:
        _audit.append(b"x" * AUDIT_LEAK_KB * 1024)
    try:
        with pool.connection() as conn, timed_dependency("postgres", "reserve"):
            conn.execute("UPDATE inventory SET reserved = reserved + %s WHERE sku = %s", (qty, sku))
    except psycopg.errors.QueryCanceled as exc:
        log.error(
            "reservation query cancelled: statement timeout",
            extra={"dependency": "postgres", "query": "reserve", "error": "QueryCanceled"},
        )
        raise HTTPException(status_code=503, detail="reservation timed out") from exc
    return {"sku": sku, "reserved": qty}
