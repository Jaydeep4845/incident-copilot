"""Run the agent on one recorded incident and save the transcript.

python -m agent.run <incident-dir> [--model openai/gpt-oss-120b] [--out runs/]
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from agent import runbooks
from agent.investigate import investigate
from agent.llm import ChatClient
from agent.telemetry import Incident


def score(report: dict | None, truth: dict) -> dict:
    if not report:
        return {"category": False, "component": False, "correct": False}
    cat = report["cause_category"] == truth["category"]
    comp = report["component"] == truth["component"]
    return {"category": cat, "component": comp, "correct": cat and comp}


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("incident")
    ap.add_argument("--model", default="openai/gpt-oss-120b")
    ap.add_argument("--provider", default="groq")
    ap.add_argument("--out", default="runs")
    args = ap.parse_args()

    inc = Incident(Path(args.incident))
    result = investigate(inc, ChatClient(args.model, args.provider), runbooks.search)
    record = asdict(result) | {
        "ground_truth": inc.ground_truth,
        "score": score(result.report, inc.ground_truth),
    }
    out = Path(args.out) / args.model.replace("/", "_")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{inc.id}.json").write_text(json.dumps(record, indent=2, default=str))

    print(f"{inc.id}: {len(result.steps)} steps, {result.tokens} tokens, {result.seconds}s")
    for s in result.steps:
        print(f"  - {s['tool']}({json.dumps(s['args'])[:90]})")
    if result.report:
        r = result.report
        print(f"REPORT: {r['cause_category']} / {r['component']} (confidence {r['confidence']})")
        print(f"  {r['root_cause']}")
        print(f"  remediation: {r['remediation']}")
    print(f"TRUTH:  {inc.ground_truth['category']} / {inc.ground_truth['component']}")
    print(f"SCORE:  {record['score']}  error={result.error}")


if __name__ == "__main__":
    main()
