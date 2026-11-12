"""Snapshot an incident's telemetry to plain files the agent can later query offline.

Output directory:
  scenario.json  ground truth, timeline, fired alerts
  metrics.json   named PromQL range queries (5 s resolution)
  logs.jsonl     service logs (structured JSON lines, plus raw text lines from libraries)
  traces.jsonl   one line per trace: spans with service, operation, duration, status
  deploys.json   deploy events written by the chaos runner
"""

from __future__ import annotations

import gzip
import json
import random
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

PROM = "http://localhost:9090/api/v1"
STEP = "5s"
W = "30s"  # rate window
QUERIES = {
    "request_rate": f"sum by (job) (rate(http_requests_total[{W}]))",
    "error_ratio": f'sum by (job) (rate(http_requests_total{{status=~"5.."}}[{W}]))'
    f" / sum by (job) (rate(http_requests_total[{W}]))",
    "latency_p95_s": "histogram_quantile(0.95, sum by (job, le) "
    f"(rate(http_request_duration_seconds_bucket[{W}])))",
    "latency_p50_s": "histogram_quantile(0.5, sum by (job, le) "
    f"(rate(http_request_duration_seconds_bucket[{W}])))",
    "route_latency_p95_s": "histogram_quantile(0.95, sum by (job, route, le) "
    f"(rate(http_request_duration_seconds_bucket[{W}])))",
    "dependency_latency_p95_s": "histogram_quantile(0.95, sum by (job, dependency, operation, le) "
    f"(rate(dependency_duration_seconds_bucket[{W}])))",
    "dependency_error_rate": "sum by (job, dependency, error) "
    f"(rate(dependency_errors_total[{W}]))",
    "db_pool_in_use": "db_pool_connections_in_use",
    "db_pool_waiting": "db_pool_requests_waiting",
    "db_pool_max": "db_pool_connections_max",
    "cache_lookups_rate": f"sum by (job, result) (rate(cache_requests_total[{W}]))",
    "memory_rss_mb": "process_resident_memory_bytes / 1e6",
    "cpu_cores": f"rate(process_cpu_seconds_total[{W}])",
    "up": "up",
    "restarts": "changes(process_start_time_seconds[1m])",
}
APP_SERVICES = ["gateway", "orders", "inventory", "payments"]


def prom(path: str, params: dict) -> dict:
    url = f"{PROM}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())["data"]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def record_metrics(start: float, end: float) -> dict:
    out = {}
    for name, query in QUERIES.items():
        data = prom("query_range", {"query": query, "start": start, "end": end, "step": STEP})
        out[name] = {
            "query": query,
            "series": [
                {
                    "labels": {k: v for k, v in s["metric"].items() if k != "instance"},
                    "values": [
                        [round(float(t), 1), float(v)] for t, v in s["values"] if v != "NaN"
                    ],
                }
                for s in data["result"]
            ],
        }
    return out


def fired_alerts(start: float, end: float) -> list[dict]:
    data = prom(
        "query_range",
        {"query": 'ALERTS{alertstate="firing"}', "start": start, "end": end, "step": STEP},
    )
    alerts = []
    for s in data["result"]:
        m = s["metric"]
        alerts.append(
            {
                "alert": m["alertname"],
                "service": m.get("job"),
                "severity": m.get("severity"),
                "first_firing": iso(float(s["values"][0][0])),
                "last_firing": iso(float(s["values"][-1][0])),
            }
        )
    return sorted(alerts, key=lambda a: a["first_firing"])


def record_logs(compose, since: float) -> list[dict]:
    res = compose(
        "logs", "--no-color", "--timestamps", "--since", iso(since), *APP_SERVICES, check=False
    )
    rows = []
    for line in res.stdout.splitlines():
        if " | " not in line:
            continue
        container, rest = line.split(" | ", 1)
        service = container.strip().rsplit("-", 1)[0]
        ts, _, text = rest.partition(" ")
        try:
            doc = json.loads(text)
            doc["ts"] = doc.get("ts", ts)
        except json.JSONDecodeError:
            if not text.strip() or "GET /metrics" in text:
                continue  # uvicorn access log noise for scrapes
            doc = {"ts": ts, "level": "RAW", "service": service, "msg": text.strip()[:500]}
        rows.append(doc)
    return sorted(rows, key=lambda d: d["ts"])


def record_traces(traces_file: Path, start: float, keep_ok: float = 0.1) -> list[dict]:
    """Group exported OTLP spans into traces.

    Keeps every error or slow trace and a sample of the rest.
    """
    spans_by_trace: dict[str, list[dict]] = defaultdict(list)
    if not traces_file.exists():
        return []
    for line in traces_file.read_text().splitlines():
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        for rs in doc.get("resourceSpans", []):
            attrs = {a["key"]: a["value"] for a in rs.get("resource", {}).get("attributes", [])}
            service = attrs.get("service.name", {}).get("stringValue", "?")
            version = attrs.get("service.version", {}).get("stringValue")
            for ss in rs.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    t0, t1 = int(sp["startTimeUnixNano"]), int(sp["endTimeUnixNano"])
                    if t0 / 1e9 < start:
                        continue
                    spans_by_trace[sp["traceId"]].append(
                        {
                            "span_id": sp["spanId"],
                            "parent_id": sp.get("parentSpanId") or None,
                            "service": service,
                            "version": version,
                            "name": sp["name"],
                            "start": iso(t0 / 1e9),
                            "duration_ms": round((t1 - t0) / 1e6, 1),
                            "error": sp.get("status", {}).get("code") == 2,
                            "status_message": sp.get("status", {}).get("message"),
                        }
                    )
    rng = random.Random(0)
    traces = []
    # Per minute and root operation keep a few error, slow and normal traces: enough evidence
    # for an investigation without storing every request.
    budget: dict[tuple, int] = defaultdict(int)
    caps = {"error": 15, "slow": 15, "ok": 5}
    items = list(spans_by_trace.items())
    rng.shuffle(items)
    for trace_id, spans in items:
        spans.sort(key=lambda s: s["start"])
        root = next((s for s in spans if s["parent_id"] is None), spans[0])
        error = any(s["error"] for s in spans)
        kind = "error" if error else "slow" if root["duration_ms"] > 500 else "ok"
        if kind == "ok" and rng.random() > keep_ok:
            continue
        key = (root["start"][:16], root["service"], root["name"], kind)
        if budget[key] >= caps[kind]:
            continue
        budget[key] += 1
        traces.append(
            {
                "trace_id": trace_id,
                "start": root["start"],
                "root": f"{root['service']} {root['name']}",
                "duration_ms": root["duration_ms"],
                "error": error,
                "spans": spans,
            }
        )
    return sorted(traces, key=lambda t: t["start"])


def baseline_health(metrics: dict, start: float, fault: float) -> dict:
    """Error ratio per service during the baseline; a recording is valid only if it is low."""
    health = {}
    for series in metrics["error_ratio"]["series"]:
        vals = [v for t, v in series["values"] if start + 30 <= t < fault]
        health[series["labels"].get("job", "?")] = round(sum(vals) / len(vals), 4) if vals else None
    worst = max((v for v in health.values() if v is not None), default=0.0)
    return {"error_ratio_by_service": health, "healthy": worst < 0.03}


def write_jsonl_gz(path: Path, rows: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def record(out: Path, start: float, end: float, meta: dict, deploys: list[dict], compose) -> None:
    metrics = record_metrics(start, end)
    meta["alerts"] = fired_alerts(start, end)
    window_start = datetime.fromisoformat(meta["window"]["start"]).timestamp()
    fault = datetime.fromisoformat(meta["window"]["fault"]).timestamp()
    meta["baseline"] = baseline_health(metrics, window_start, fault)
    (out / "metrics.json").write_text(json.dumps(metrics))
    (out / "deploys.json").write_text(json.dumps(deploys, indent=2))
    (out / "scenario.json").write_text(json.dumps(meta, indent=2))
    logs = record_logs(compose, start)
    write_jsonl_gz(out / "logs.jsonl.gz", logs)
    traces = record_traces(
        Path(__file__).resolve().parents[1] / "infra" / "out" / "traces.jsonl", start
    )
    write_jsonl_gz(out / "traces.jsonl.gz", traces)
    print(
        f"recorded: {len(metrics)} metric queries, {len(meta['alerts'])} alerts, "
        f"{len(logs)} log lines, {len(traces)} traces, {len(deploys)} deploys, "
        f"baseline healthy: {meta['baseline']['healthy']}",
        flush=True,
    )
