"""Build the demo's data file from recordings and benchmark results.

  python demo/build_data.py <recordings-dir> <results-dir> --model agent__openai_gpt-oss-120b

For each incident: the alert, a few metric series for charts, the deploy list, the chosen model's
investigation transcript and report, the baseline's answer and the ground truth.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.telemetry import Incident

CHART_METRICS = ["error_ratio", "latency_p95_s"]
HERE = Path(__file__).parent


def compact(value, limit: int = 1200):
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else text[:limit] + " …"


def series(inc: Incident, name: str) -> list[dict]:
    out = []
    for s in inc.metric_series(name):
        job = s["labels"].get("job")
        if not job:
            continue
        out.append(
            {"service": job, "points": [[round(t), round(v, 4)] for t, v in s["values"][::2]]}
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("recordings", type=Path)
    ap.add_argument("results", type=Path)
    ap.add_argument("--model", default="agent__openai_gpt-oss-120b")
    args = ap.parse_args()

    summary = json.loads((args.results / "summary.json").read_text())
    incidents = []
    for d in sorted(p for p in args.recordings.iterdir() if (p / "scenario.json").exists()):
        inc = Incident(d)
        agent_file = args.results / args.model / f"{inc.id}__run0.json"
        base_file = args.results / "last-deploy" / f"{inc.id}__run0.json"
        if not agent_file.exists():
            continue
        agent = json.loads(agent_file.read_text())
        base = json.loads(base_file.read_text()) if base_file.exists() else None
        incidents.append(
            {
                "id": inc.id,
                "difficulty": inc.meta.get("difficulty"),
                "alert": inc.alert,
                "now": inc.now.isoformat(),
                "charts": {m: series(inc, m) for m in CHART_METRICS},
                "deploys": inc.deploys(),
                "steps": [
                    {
                        "tool": s["tool"],
                        "args": s["args"],
                        "result": compact(s["result"]),
                        "thought": (s.get("thought") or "")[:600],
                    }
                    for s in agent["steps"]
                ],
                "report": agent["report"],
                "score": agent["score"],
                "tokens": agent["tokens"],
                "seconds": agent["seconds"],
                "model": agent["model"],
                "baseline": base and {"report": base["report"], "score": base["score"]},
                "ground_truth": inc.ground_truth,
            }
        )
    out = HERE / "site" / "data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"incidents": incidents, "benchmark": summary}, default=str))
    print(f"{len(incidents)} incidents -> {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
