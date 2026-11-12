"""Run one incident scenario on the Docker Compose system and record its telemetry.

  python chaos/run_scenario.py <scenario-id> <output-dir>

Timeline: start system -> warm-up -> baseline traffic -> fault steps -> fault window
-> record -> tear down.
Needs Docker and PyYAML; designed for a GitHub Actions ubuntu runner.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
sys.path.insert(0, str(ROOT / "chaos"))
from record import record  # noqa: E402


def log(msg: str) -> None:
    print(f"[{datetime.now(UTC):%H:%M:%S}] {msg}", flush=True)


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args], cwd=INFRA, check=check, text=True, capture_output=True
    )


def read_env(service: str) -> dict[str, str]:
    lines = (INFRA / "runtime" / f"{service}.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def write_env(service: str, env: dict[str, str]) -> None:
    (INFRA / "runtime" / f"{service}.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))


def wait_http(url: str, timeout_s: float = 180) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3):
                return
        except Exception:
            time.sleep(2)
    raise TimeoutError(f"{url} not ready after {timeout_s}s")


def wait_end_to_end(timeout_s: float = 240) -> None:
    """Block until a real order lookup and a real checkout succeed through the whole system."""
    body = json.dumps(
        {"user_id": 1, "sku": "SKU-001", "qty": 1, "amount": 10.0, "currency": "USD"}
    ).encode()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://localhost:8000/orders/1", timeout=5):
                pass
            req = urllib.request.Request(
                "http://localhost:8000/checkout",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10):
                log("end-to-end check passed")
                return
        except Exception:
            time.sleep(3)
    raise TimeoutError("system never became healthy end to end")


def run_step(step: dict, deploys: list[dict]) -> None:
    action = step["action"]
    if action == "deploy":
        svc = step["service"]
        env = read_env(svc)
        old = env.get("APP_VERSION")
        env.update({k: str(v) for k, v in step.get("set", {}).items()})
        env["APP_VERSION"] = str(step["version"])
        write_env(svc, env)
        compose("up", "-d", "--no-deps", "--force-recreate", svc)
        deploys.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "service": svc,
                "from_version": old,
                "to_version": str(step["version"]),
                "change": step["change"],
                "commit": f"{random.getrandbits(28):07x}",
                "config_changed": sorted(step.get("set", {}).keys()),
            }
        )
        log(f"deployed {svc} {old} -> {step['version']}")
    elif action == "sql":
        compose("exec", "-T", "postgres", "psql", "-U", "app", "-d", "shop", "-c", step["sql"])
        log(f"sql: {step['sql']}")
    elif action == "sql_background":
        compose("exec", "-d", "postgres", "psql", "-U", "app", "-d", "shop", "-c", step["sql"])
        log(f"sql (background): {step['sql']}")
    elif action in ("pause", "stop"):
        compose(action, step["service"])
        log(f"{action} {step['service']}")
    elif action == "http":
        req = urllib.request.Request(step["url"], method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            log(f"http {step['url']} -> {r.status}")
    else:
        raise ValueError(f"unknown action {action}")


def main(scenario_id: str, out_dir: str) -> None:
    spec = yaml.safe_load((ROOT / "chaos" / "scenarios.yaml").read_text())
    defaults = spec["defaults"]
    scenario = next(s for s in spec["scenarios"] if s["id"] == scenario_id)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    runtime = INFRA / "runtime"
    shutil.rmtree(runtime, ignore_errors=True)
    shutil.copytree(INFRA / "defaults", runtime)
    (INFRA / "out").mkdir(exist_ok=True)

    log(f"scenario {scenario_id}: starting system")
    compose("up", "-d", "--build")
    wait_http("http://localhost:8000/health")
    wait_end_to_end()
    wait_http("http://localhost:9090/-/ready")
    log(f"warm-up {defaults['warmup_s']}s")
    time.sleep(defaults["warmup_s"])

    t_start = time.time()
    t_fault = t_start + defaults["baseline_s"]
    t_end = t_fault + defaults["fault_s"]
    deploys: list[dict] = []
    for step in sorted(scenario["steps"], key=lambda s: s["at"]):
        delay = t_fault + step["at"] - time.time()
        if delay > 0:
            time.sleep(delay)
        run_step(step, deploys)
    time.sleep(max(0.0, t_end - time.time()))
    log("recording")

    meta = {
        "id": scenario_id,
        "difficulty": scenario.get("difficulty"),
        "ground_truth": scenario["ground_truth"],
        "steps": scenario["steps"],
        "window": {
            "start": datetime.fromtimestamp(t_start, UTC).isoformat(),
            "fault": datetime.fromtimestamp(t_fault, UTC).isoformat(),
            "end": datetime.fromtimestamp(t_end, UTC).isoformat(),
        },
    }
    record(out, t_start - 60, t_end, meta, deploys, compose)
    compose("down", "-v", check=False)
    if not meta["baseline"]["healthy"]:
        log(f"WARNING: unhealthy baseline {meta['baseline']}")
        sys.exit(2)
    log(f"done: {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
