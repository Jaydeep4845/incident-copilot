"""Snapshot an incident's telemetry to plain files the agent can later query offline.

Output directory:
  scenario.json  ground truth, timeline, fired alerts
  metrics.json   named PromQL range queries (5 s resolution)
  logs.jsonl     service logs (structured JSON lines, plus raw text lines from libraries)
  traces.jsonl   one line per trace: spans with service, operation, duration, status
  deploys.json   deploy events written by the chaos runner
"""

from __future__ import annotations

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
    for trace_id, spans in spans_by_trace.items():
        spans.sort(key=lambda s: s["start"])
        root = next((s for s in spans if s["parent_id"] is None), spans[0])
        interesting = any(s["error"] for s in spans) or root["duration_ms"] > 500
        if interesting or rng.random() < keep_ok:
            traces.append(
                {
                    "trace_id": trace_id,
                    "start": root["start"],
                    "root": f"{root['service']} {root['name']}",
                    "duration_ms": root["duration_ms"],
                    "error": any(s["error"] for s in spans),
                    "spans": spans,
                }
            )
    return sorted(traces, key=lambda t: t["start"])


def record(out: Path, start: float, end: float, meta: dict, deploys: list[dict], compose) -> None:
    metrics = record_metrics(start, end)
    meta["alerts"] = fired_alerts(start, end)
    (out / "metrics.json").write_text(json.dumps(metrics))
    (out / "deploys.json").write_text(json.dumps(deploys, indent=2))
    (out / "scenario.json").write_text(json.dumps(meta, indent=2))
    logs = record_logs(compose, start)
    with (out / "logs.jsonl").open("w") as f:
        for row in logs:
            f.write(json.dumps(row) + "\n")
    traces = record_traces(
        Path(__file__).resolve().parents[1] / "infra" / "out" / "traces.jsonl", start
    )
    with (out / "traces.jsonl").open("w") as f:
        for t in traces:
            f.write(json.dumps(t) + "\n")
    print(
        f"recorded: {len(metrics)} metric queries, {len(meta['alerts'])} alerts, "
        f"{len(logs)} log lines, {len(traces)} traces, {len(deploys)} deploys",
        flush=True,
    )
