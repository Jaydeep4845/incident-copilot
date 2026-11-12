"""Investigation tools. Each returns a compact, citeable summary (never raw dumps) with evidence ids
(M=metric, L=log group, T=trace, D=deploy, R=runbook, O=overview) that the final report must cite.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from agent.telemetry import APP_SERVICES, TOPOLOGY, Incident, parse_ts

METRICS = {
    "request_rate": "requests per second, by service (job)",
    "error_ratio": "share of requests returning 5xx, by service",
    "latency_p95_s": "p95 request latency in seconds, by service",
    "route_latency_p95_s": "p95 latency in seconds, by service and route",
    "dependency_latency_p95_s": "p95 latency of calls to postgres (query time only, excluding pool wait), redis, payment-provider, by caller",
    "dependency_error_rate": "failed dependency calls per second, by caller, dependency and error type",
    "db_pool_in_use": "DB connections checked out, by service (orders, inventory)",
    "db_pool_waiting": "requests waiting for a DB connection, by service",
    "db_pool_max": "DB pool size, by service",
    "cache_lookups_rate": "inventory stock-cache lookups per second by result (hit, miss, error)",
    "memory_rss_mb": "resident memory in MB, by service",
    "cpu_cores": "CPU cores used, by service",
    "up": "1 if the service's metrics endpoint is reachable, by service",
    "restarts": "process restarts in the last minute, by service",
}
RUNBOOK_DIR = Path(__file__).resolve().parents[1] / "runbooks"


def hms(ts) -> str:
    return ts.strftime("%H:%M:%S")


def template(msg: str) -> str:
    msg = re.sub(r"\b[0-9a-f]{12,}\b", "<id>", msg)
    msg = re.sub(r"\d+(\.\d+)?", "<n>", msg)
    return msg[:160]


class Toolbox:
    """All tools for one investigation. Keeps track of the evidence ids it has handed out."""

    def __init__(self, incident: Incident, runbook_search=None):
        self.inc = incident
        self.evidence: dict[str, str] = {}
        self._counters: dict[str, int] = defaultdict(int)
        self._runbooks = runbook_search

    def _id(self, prefix: str, summary: str) -> str:
        self._counters[prefix] += 1
        eid = f"{prefix}{self._counters[prefix]}"
        self.evidence[eid] = summary
        return eid

    def _since(self, minutes: float):
        return self.inc.now - timedelta(minutes=minutes)

    # ------------------------------------------------------------------ tools
    def get_alert(self) -> dict:
        a = self.inc.alert
        return {
            "alert": a["alert"],
            "service": a["service"],
            "first_firing": a["first_firing"],
            "now": self.inc.now.isoformat(),
            "other_alerts_firing": [
                f"{x['alert']} on {x['service']} since {x['first_firing'][11:19]}"
                for x in self.inc.meta["alerts"]
                if x is not a and parse_ts(x["first_firing"]) <= self.inc.now
            ],
        }

    def describe_system(self) -> dict:
        return TOPOLOGY

    def service_overview(self) -> dict:
        """5+ minutes ago vs last 3 minutes for each service: request rate, errors, p95 latency."""
        recent_from = (self.inc.now - timedelta(minutes=3)).timestamp()
        earlier_to = (self.inc.now - timedelta(minutes=5)).timestamp()
        rows = {}
        for metric in ("request_rate", "error_ratio", "latency_p95_s"):
            for s in self.inc.metric_series(metric):
                svc = s["labels"].get("job")
                early = [v for t, v in s["values"] if t <= earlier_to]
                late = [v for t, v in s["values"] if t >= recent_from]
                rows.setdefault(svc, {})[metric] = {
                    "earlier": round(statistics.fmean(early), 3) if early else None,
                    "last_3_min": round(statistics.fmean(late), 3) if late else None,
                }
        eid = self._id("O", "service overview: " + str(rows))
        return {"id": eid, "services": rows}

    def list_metrics(self) -> dict:
        return METRICS

    def query_metric(self, metric: str, service: str | None = None, minutes: float = 15) -> dict:
        if metric not in METRICS:
            return {"error": f"unknown metric {metric}; use one of {list(METRICS)}"}
        start = self._since(minutes).timestamp()
        out = []
        for s in self.inc.metric_series(metric):
            if service and service not in s["labels"].values():
                continue
            vals = [(t, v) for t, v in s["values"] if t >= start]
            if not vals:
                continue
            split = max(1, len(vals) // 2)
            base = [v for _, v in vals[:split]]
            mean, sd = statistics.fmean(base), (statistics.pstdev(base) if len(base) > 1 else 0.0)
            threshold = max(3 * sd, 0.5 * abs(mean), 0.01)
            changed = next((t for t, v in vals[split:] if abs(v - mean) > threshold), None)
            buckets = [vals[i * len(vals) // 8 : (i + 1) * len(vals) // 8] for i in range(8)]
            spark = [round(statistics.fmean(v for _, v in b), 3) for b in buckets if b]
            summary = {
                "labels": s["labels"],
                "first_half_avg": round(mean, 3),
                "last_value": round(vals[-1][1], 3),
                "max": round(max(v for _, v in vals), 3),
                "changed_at": hms(parse_ts_epoch(changed)) if changed else None,
                f"trend_{minutes:g}min_8_points": spark,
            }
            summary["id"] = self._id("M", f"{metric} {s['labels']}: {summary}")
            out.append(summary)
        if out:
            return {"metric": metric, "series": out[:8]}
        available = sorted(
            {v for s in self.inc.metric_series(metric) for v in s["labels"].values()}
        )
        return {
            "metric": metric,
            "series": [],
            "note": f"no series match service={service!r}; label values available: {available}",
        }

    def search_logs(
        self,
        service: str | None = None,
        level: str | None = None,
        contains: str | None = None,
        minutes: float = 15,
        limit: int = 8,
    ) -> dict:
        since = self._since(minutes)
        groups: dict[tuple, dict] = {}
        for r in self.inc.logs():
            if service and r.get("service") != service:
                continue
            if level and r.get("level", "").upper() != level.upper():
                continue
            msg = r.get("msg", "")
            if contains and contains.lower() not in msg.lower():
                continue
            ts = parse_ts(r["ts"])
            if ts < since:
                continue
            key = (r.get("service"), r.get("level"), template(msg))
            g = groups.setdefault(
                key, {"count": 0, "first": ts, "last": ts, "example": msg[:160], "version": None}
            )
            g["count"] += 1
            g["last"] = max(g["last"], ts)
            g["first"] = min(g["first"], ts)
            g["version"] = r.get("version") or g["version"]
        ranked = sorted(groups.items(), key=lambda kv: -kv[1]["count"])[:limit]
        results = []
        for (svc, lvl, _), g in ranked:
            row = {
                "service": svc,
                "level": lvl,
                "count": g["count"],
                "first_seen": hms(g["first"]),
                "last_seen": hms(g["last"]),
                "service_version": g["version"],
                "example": g["example"],
            }
            row["id"] = self._id(
                "L", f"{svc} {lvl} x{g['count']} from {row['first_seen']}: {g['example']}"
            )
            results.append(row)
        return {"matching_groups": len(groups), "top": results}

    def get_traces(
        self, service: str | None = None, kind: str = "error", minutes: float = 10, limit: int = 3
    ) -> dict:
        since = self._since(minutes)
        picked = []
        for t in reversed(self.inc.traces()):
            if parse_ts(t["start"]) < since:
                break
            if service and not any(sp["service"] == service for sp in t["spans"]):
                continue
            if kind == "error" and not t["error"]:
                continue
            if kind == "slow" and t["duration_ms"] < 500:
                continue
            picked.append(t)
            if len(picked) >= limit:
                break
        out = []
        for t in picked:
            spans = sorted(t["spans"], key=lambda s: -s["duration_ms"])[:4]
            row = {
                "root": t["root"],
                "start": t["start"][11:19],
                "duration_ms": t["duration_ms"],
                "error": t["error"],
                "longest_spans": [
                    {
                        "service": s["service"],
                        "version": s.get("version"),
                        "span": s["name"],
                        "ms": s["duration_ms"],
                        **({"error": s.get("status_message") or True} if s["error"] else {}),
                    }
                    for s in spans
                ],
            }
            row["id"] = self._id(
                "T", f"trace {t['root']} {t['duration_ms']} ms: {row['longest_spans'][:3]}"
            )
            out.append(row)
        return {"kind": kind, "traces": out}

    def list_deploys(self, minutes: float = 60) -> dict:
        since = self._since(minutes)
        out = []
        for d in self.inc.deploys():
            if parse_ts(d["ts"]) < since:
                continue
            row = {
                "time": d["ts"][11:19],
                "service": d["service"],
                "from": d["from_version"],
                "to": d["to_version"],
                "change": d["change"],
                "config_changed": d["config_changed"],
                "commit": d["commit"],
            }
            row["id"] = self._id(
                "D", f"deploy {d['service']} {d['to_version']} at {row['time']}: {d['change']}"
            )
            out.append(row)
        return {"deploys": out, "note": "no deploys in this window" if not out else None}

    def search_runbooks(self, query: str, k: int = 2) -> dict:
        if self._runbooks is None:
            return {"error": "runbook search unavailable"}
        out = []
        for hit in self._runbooks(query, k):
            hit["id"] = self._id("R", f"runbook {hit['runbook']} / {hit['section']}")
            out.append(hit)
        return {"results": out}


def parse_ts_epoch(t: float):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(t, UTC)


# OpenAI-style function definitions for tool calling
TOOL_SPECS = [
    {
        "name": "get_alert",
        "description": "The alert that paged you, the current time and other firing alerts.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "describe_system",
        "description": "Services, what each one calls, and notes.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "service_overview",
        "description": "Request rate, error ratio and p95 latency per service: earlier vs last 3 minutes.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_metrics",
        "description": "Names and meaning of the metrics you can query.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "query_metric",
        "description": "Summarise a metric over the last N minutes: average, last value, max, when it changed.",
        "parameters": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": list(METRICS)},
                "service": {
                    "type": "string",
                    "description": "filter by service or dependency name",
                },
                "minutes": {"type": "number", "default": 15},
            },
            "required": ["metric"],
        },
    },
    {
        "name": "search_logs",
        "description": "Log messages grouped by pattern with counts and first/last seen.",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "enum": APP_SERVICES},
                "level": {"type": "string", "enum": ["ERROR", "WARNING", "INFO", "RAW"]},
                "contains": {"type": "string"},
                "minutes": {"type": "number", "default": 15},
                "limit": {"type": "integer", "default": 8},
            },
        },
    },
    {
        "name": "get_traces",
        "description": "Recent request traces with their longest spans (where the time went) and errors.",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "enum": APP_SERVICES},
                "kind": {"type": "string", "enum": ["error", "slow", "any"], "default": "error"},
                "minutes": {"type": "number", "default": 10},
                "limit": {"type": "integer", "default": 3},
            },
        },
    },
    {
        "name": "list_deploys",
        "description": "Deploys in the last N minutes with version and change summary.",
        "parameters": {
            "type": "object",
            "properties": {"minutes": {"type": "number", "default": 60}},
        },
    },
    {
        "name": "search_runbooks",
        "description": "Search the team's runbooks for guidance.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "k": {"type": "integer", "default": 2}},
            "required": ["query"],
        },
    },
]
