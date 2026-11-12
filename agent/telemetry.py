"""Read-only access to one recorded incident, as an on-call engineer would see it at "now".

The agent never sees the ground truth or the injected steps, and never sees data after the
investigation time (alert time + a few minutes): no peeking into the future.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# The system as documented for on-call engineers (not derived from the recording).
TOPOLOGY = {
    "gateway": {"calls": ["orders"], "notes": "public API: POST /checkout, GET /orders/{user_id}"},
    "orders": {
        "calls": ["inventory", "payments", "postgres"],
        "notes": "holds a DB connection for the whole checkout transaction; pool of N connections",
    },
    "inventory": {
        "calls": ["redis", "postgres"],
        "notes": "stock cache in Redis, falls back to DB",
    },
    "payments": {"calls": ["payment-provider"], "notes": "external card processor"},
    "postgres": {"calls": [], "notes": "orders (4M rows) and inventory tables"},
    "redis": {"calls": [], "notes": "stock cache"},
    "payment-provider": {"calls": [], "notes": "external third party"},
}
APP_SERVICES = ["gateway", "orders", "inventory", "payments"]
INVESTIGATION_DELAY_S = 120


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def read_jsonl_gz(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@dataclass
class Incident:
    root: Path
    meta: dict = field(init=False)
    alert: dict = field(init=False)
    now: datetime = field(init=False)
    window_start: datetime = field(init=False)

    def __post_init__(self) -> None:
        self.meta = json.loads((self.root / "scenario.json").read_text())
        fault = parse_ts(self.meta["window"]["fault"])
        end = parse_ts(self.meta["window"]["end"])
        # The page the on-call engineer receives: the first alert that fired after the fault.
        fired = [a for a in self.meta["alerts"] if parse_ts(a["first_firing"]) >= fault]
        if not fired:
            raise ValueError(f"{self.root.name}: no alert fired after the fault")
        priority = {s: i for i, s in enumerate(APP_SERVICES)}
        fired.sort(key=lambda a: (a["first_firing"], priority.get(a["service"], 9)))
        self.alert = fired[0]
        self.now = min(
            parse_ts(self.alert["first_firing"]) + timedelta(seconds=INVESTIGATION_DELAY_S), end
        )
        self.window_start = parse_ts(self.meta["window"]["start"])
        self._metrics = json.loads((self.root / "metrics.json").read_text())
        self._deploys = json.loads((self.root / "deploys.json").read_text())
        self._logs = [
            r for r in read_jsonl_gz(self.root / "logs.jsonl.gz") if parse_ts(r["ts"]) <= self.now
        ]
        self._traces = [
            t
            for t in read_jsonl_gz(self.root / "traces.jsonl.gz")
            if parse_ts(t["start"]) <= self.now
        ]

    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def ground_truth(self) -> dict:
        """For scoring only: never passed to the agent."""
        return self.meta["ground_truth"]

    def metric_series(self, name: str) -> list[dict]:
        now = self.now.timestamp()
        out = []
        for s in self._metrics.get(name, {}).get("series", []):
            values = [(t, v) for t, v in s["values"] if t <= now]
            if values:
                out.append({"labels": s["labels"], "values": values})
        return out

    def logs(self) -> list[dict]:
        return self._logs

    def traces(self) -> list[dict]:
        return self._traces

    def deploys(self) -> list[dict]:
        return [d for d in self._deploys if parse_ts(d["ts"]) <= self.now]
