"""Baselines the agent must beat.

1. last_deploy: "blame the most recent deploy", the shortcut many on-call engineers take first.
2. single_prompt: one LLM call with a fixed digest of the most useful telemetry, no investigation.
"""

from __future__ import annotations

import json
import time

from pydantic import ValidationError

from agent.investigate import SUBMIT_SPEC, SYSTEM_PROMPT, InvestigationResult, finish
from agent.llm import ChatClient
from agent.schemas import RCAReport
from agent.telemetry import Incident, parse_ts
from agent.tools import Toolbox


def last_deploy(incident: Incident) -> InvestigationResult:
    alert_time = parse_ts(incident.alert["first_firing"])
    deploys = [d for d in incident.deploys() if parse_ts(d["ts"]) <= alert_time]
    if deploys:
        d = deploys[-1]
        category = "bad_deploy_config" if d["config_changed"] else "bad_deploy_code"
        component, cause = d["service"], f"most recent deploy: {d['service']} {d['to_version']}"
    else:
        category, component = "dependency_down", incident.alert["service"]
        cause = "no recent deploy; assume the alerting service is failing"
    report = {
        "cause_category": category,
        "component": component,
        "root_cause": cause,
        "evidence": [],
        "confidence": 0.5,
        "remediation": {
            "action": "rollback_deploy" if deploys else "escalate",
            "target": component,
        },
    }
    return InvestigationResult(incident=incident.id, model="baseline:last-deploy", report=report)


def digest(box: Toolbox) -> dict:
    """The telemetry an engineer would glance at first, gathered without any reasoning."""
    return {
        "alert": box.get_alert(),
        "system": box.describe_system(),
        "overview": box.service_overview(),
        "deploys": box.list_deploys(),
        "error_logs": box.search_logs(level="ERROR", limit=10),
        "warning_logs": box.search_logs(level="WARNING", limit=5),
        "error_traces": box.get_traces(kind="error", limit=2),
        "slow_traces": box.get_traces(kind="slow", limit=2),
        "dependency_latency": box.query_metric("dependency_latency_p95_s"),
        "dependency_errors": box.query_metric("dependency_error_rate"),
        "cpu": box.query_metric("cpu_cores"),
        "memory": box.query_metric("memory_rss_mb"),
        "db_pool": box.query_metric("db_pool_in_use"),
    }


def single_prompt(incident: Incident, client: ChatClient) -> InvestigationResult:
    box = Toolbox(incident)
    started = time.time()
    result = InvestigationResult(
        incident=incident.id, model=f"single-prompt:{client.model}", report=None
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "You have been paged. Here is a snapshot of the telemetry (no further tools are "
            "available). Determine the root cause and call submit_report.\n\n"
            + json.dumps(digest(box), default=str),
        },
    ]
    for _ in range(3):
        msg = client.chat(
            messages,
            tools=[SUBMIT_SPEC],
            tool_choice={"type": "function", "function": {"name": "submit_report"}},
        )
        call = (msg.get("tool_calls") or [None])[0]
        if not call:
            continue
        try:
            report = RCAReport.model_validate(json.loads(call["function"]["arguments"]))
            if report.unknown_evidence(set(box.evidence)):
                raise ValueError("unknown evidence ids")
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            messages += [
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": f"rejected: {str(exc)[:300]}",
                },
            ]
            continue
        result.report = report.model_dump(mode="json")
        return finish(result, client, started)
    result.error = "no valid report"
    return finish(result, client, started)
