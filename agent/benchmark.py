"""Benchmark the agent and the baselines on every recorded incident.

  python -m agent.benchmark <recordings-dir> --systems agent:openai/gpt-oss-120b single:openai/gpt-oss-120b last-deploy
  python -m agent.benchmark <recordings-dir> --report    # summary table from saved results

Results are saved per (system, incident, run) and reused, so the free daily LLM quota can be
spread over several days without redoing work.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from agent import runbooks
from agent.baselines import last_deploy, single_prompt
from agent.investigate import investigate
from agent.llm import ChatClient, QuotaExhausted
from agent.run import score
from agent.telemetry import Incident

RESULTS = Path("results")


def incidents(root: Path) -> list[Incident]:
    out = []
    for d in sorted(p for p in root.iterdir() if (p / "scenario.json").exists()):
        try:
            inc = Incident(d)
        except ValueError as exc:
            print(f"skip {d.name}: {exc}")
            continue
        if inc.meta.get("baseline", {}).get("healthy", True):
            out.append(inc)
    return out


def run_system(system: str, inc: Incident, provider: str):
    kind, _, model = system.partition(":")
    if kind == "last-deploy":
        return last_deploy(inc)
    client = ChatClient(model, provider)
    if kind == "single":
        return single_prompt(inc, client)
    return investigate(inc, client, runbooks.search)


def path_for(system: str, inc_id: str, run: int) -> Path:
    return RESULTS / system.replace("/", "_").replace(":", "__") / f"{inc_id}__run{run}.json"


def bench(root: Path, systems: list[str], runs: int, provider: str) -> None:
    for system in systems:
        for inc in incidents(root):
            for run in range(runs if not system.startswith("last-deploy") else 1):
                out = path_for(system, inc.id, run)
                if out.exists():
                    continue
                try:
                    res = run_system(system, inc, provider)
                except QuotaExhausted as exc:
                    print(f"{system}: daily quota used up, stopping this system ({exc})")
                    break
                rec = asdict(res) | {
                    "system": system,
                    "run": run,
                    "difficulty": inc.meta.get("difficulty"),
                    "ground_truth": inc.ground_truth,
                    "score": score(res.report, inc.ground_truth),
                }
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(rec, indent=2, default=str))
                s = rec["score"]
                print(
                    f"{system:38s} {inc.id:28s} run{run} "
                    f"{'OK ' if s['correct'] else 'MISS'} cat={s['category']!s:5s} comp={s['component']!s:5s} "
                    f"steps={len(res.steps)} tokens={res.tokens}",
                    flush=True,
                )
            else:
                continue
            break


def bootstrap(values: list[float], n: int = 2000) -> tuple[float, float]:
    rng = random.Random(0)
    means = sorted(sum(rng.choices(values, k=len(values))) / len(values) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def report() -> dict:
    table = {}
    for d in sorted(RESULTS.iterdir()):
        recs = [json.loads(p.read_text()) for p in d.glob("*.json")]
        if not recs:
            continue
        correct = [float(r["score"]["correct"]) for r in recs]
        comp = [float(r["score"]["component"]) for r in recs]
        lo, hi = bootstrap(correct)
        table[recs[0]["system"]] = {
            "n": len(recs),
            "root_cause_accuracy": round(sum(correct) / len(correct), 3),
            "ci95": [round(lo, 3), round(hi, 3)],
            "component_accuracy": round(sum(comp) / len(comp), 3),
            "avg_steps": round(sum(len(r["steps"]) for r in recs) / len(recs), 1),
            "avg_tokens": round(sum(r["tokens"] for r in recs) / len(recs)),
            "avg_seconds": round(sum(r["seconds"] for r in recs) / len(recs), 1),
            "by_difficulty": {
                k: round(
                    sum(r["score"]["correct"] for r in recs if r["difficulty"] == k)
                    / max(1, sum(1 for r in recs if r["difficulty"] == k)),
                    3,
                )
                for k in ("easy", "medium", "hard")
            },
        }
    for system, row in table.items():
        print(f"{system:40s} {row}")
    (RESULTS / "summary.json").write_text(json.dumps(table, indent=2))
    return table


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("recordings", type=Path)
    ap.add_argument("--systems", nargs="*", default=["last-deploy"])
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--provider", default="groq")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.report:
        report()
    else:
        bench(args.recordings, args.systems, args.runs, args.provider)
        report()


if __name__ == "__main__":
    main()
