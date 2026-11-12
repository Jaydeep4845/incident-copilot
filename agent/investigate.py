"""The investigation loop: a plain tool-calling loop, bounded, with a validated final report.

No agent framework: the control flow is small and explicit, which makes it easy to test, trace and bound.
"""

from __future__ import annotations

import inspect
import json
import time
from dataclasses import dataclass, field

from pydantic import ValidationError

from agent.llm import ChatClient
from agent.schemas import CauseCategory, Component, RCAReport, RemediationAction
from agent.telemetry import Incident
from agent.tools import TOOL_SPECS, Toolbox

MAX_STEPS = 14
MAX_REPORT_RETRIES = 2

SYSTEM_PROMPT = """You are an experienced on-call site reliability engineer investigating a production alert
in a small e-commerce system. You can only learn about the system through the tools.

How to work:
- Start from the alert, then get an overview of all services before going deep.
- Early on, scan ERROR and WARNING logs across all services (search_logs without a service filter):
  log messages often name the failing dependency and the error type directly.
- Separate symptoms from the root cause. The service that paged is often not the cause: follow the
  dependency chain (describe_system) to where the problem starts.
- Check recent deploys, but treat a deploy as the cause only if the timing AND the failing behaviour point
  to it (for example, errors from the deployed service's own code or settings). Unrelated deploys happen.
- Confirm a hypothesis with at least two independent kinds of evidence (metrics, logs, traces, deploys).
- If you are unsure what a symptom means, search the runbooks.
- Be efficient: a few targeted tool calls are better than many broad ones; do not repeat a call.

When you are confident, call submit_report. Cite only evidence ids returned by tools (M, L, T, D, R, O).
The remediation must be one of the allowed actions and always needs human approval."""

CATEGORY_GUIDE = (
    "bad_deploy_code: a release introduced a code bug (exceptions, wrong behaviour). "
    "bad_deploy_config: a release changed settings (timeouts, pool sizes, retries, credentials) that cause the failure. "
    "db_missing_index: queries became slow because an index is missing (full table scans). "
    "db_lock_contention: queries wait on locks held by another transaction. "
    "dependency_down: a dependency fails on essentially every call (crashed, refusing connections, not responding, "
    "every call timing out) or rejects a large share of requests with errors. "
    "slow_dependency: a dependency still answers correctly but much slower than normal. "
    "resource_exhaustion: CPU or memory of a service is exhausted without a bad release (e.g. runaway job)."
)

SUBMIT_SPEC = {
    "name": "submit_report",
    "description": "Submit the final root-cause report.",
    "parameters": {
        "type": "object",
        "properties": {
            "cause_category": {
                "type": "string",
                "enum": [c.value for c in CauseCategory],
                "description": CATEGORY_GUIDE,
            },
            "component": {
                "type": "string",
                "enum": [c.value for c in Component],
                "description": "where the root cause is, not where symptoms show",
            },
            "root_cause": {"type": "string"},
            "evidence": {
                "type": "array",
                "minItems": 2,
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "finding": {"type": "string"}},
                    "required": ["id", "finding"],
                },
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "remediation": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": [a.value for a in RemediationAction]},
                    "target": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["action", "rationale"],
            },
        },
        "required": [
            "cause_category",
            "component",
            "root_cause",
            "evidence",
            "confidence",
            "remediation",
        ],
    },
}


@dataclass
class InvestigationResult:
    incident: str
    model: str
    report: dict | None
    steps: list[dict] = field(default_factory=list)
    tokens: int = 0
    llm_calls: int = 0
    seconds: float = 0.0
    error: str | None = None


def call_tool(box: Toolbox, name: str, args: dict) -> dict:
    fn = getattr(box, name, None)
    if fn is None or name.startswith("_"):
        return {"error": f"unknown tool {name}"}
    accepted = set(inspect.signature(fn).parameters)
    ignored = sorted(set(args) - accepted)
    try:
        out = fn(**{k: v for k, v in args.items() if k in accepted})
    except (TypeError, ValueError) as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    if ignored and isinstance(out, dict):
        out = {**out, "note": f"ignored unsupported arguments {ignored}"}
    return out


def investigate(incident: Incident, client: ChatClient, runbook_search=None) -> InvestigationResult:
    box = Toolbox(incident, runbook_search)
    tools = TOOL_SPECS + [SUBMIT_SPEC]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"You have been paged: {incident.alert['alert']} on "
            f"{incident.alert['service']}. Investigate and submit a root-cause report.",
        },
    ]
    result = InvestigationResult(incident=incident.id, model=client.model, report=None)
    started = time.time()
    report_retries = 0
    for step in range(MAX_STEPS + 1):
        force = step == MAX_STEPS
        if force:
            messages.append(
                {"role": "user", "content": "Step budget reached: submit your report now."}
            )
        msg = client.chat(
            messages,
            tools=[SUBMIT_SPEC] if force else tools,
            tool_choice={"type": "function", "function": {"name": "submit_report"}}
            if force
            else None,
        )
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        calls = msg.get("tool_calls") or []
        if not calls:
            messages.append({"role": "user", "content": "Use the tools, or call submit_report."})
            continue
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "submit_report":
                try:
                    report = RCAReport.model_validate(args)
                    unknown = report.unknown_evidence(set(box.evidence))
                    if unknown:
                        raise ValueError(f"evidence ids {unknown} were never returned by a tool")
                except (ValidationError, ValueError) as exc:
                    report_retries += 1
                    feedback = {"error": f"report rejected: {str(exc)[:400]}. Fix it and resubmit."}
                    result.steps.append({"tool": name, "args": args, "result": feedback})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(feedback),
                        }
                    )
                    if report_retries > MAX_REPORT_RETRIES:
                        result.error = "report validation failed"
                        return finish(result, client, started)
                    continue
                result.report = report.model_dump(mode="json")
                result.report["evidence_detail"] = {
                    e.id: box.evidence[e.id] for e in report.evidence
                }
                result.steps.append(
                    {
                        "tool": name,
                        "args": args,
                        "result": "accepted",
                        "thought": msg.get("content"),
                    }
                )
                return finish(result, client, started)
            output = call_tool(box, name, args)
            result.steps.append(
                {"tool": name, "args": args, "result": output, "thought": msg.get("content")}
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(output, default=str),
                }
            )
    result.error = "no report submitted"
    return finish(result, client, started)


def finish(result: InvestigationResult, client: ChatClient, started: float) -> InvestigationResult:
    result.tokens = client.usage.total
    result.llm_calls = client.usage.calls
    result.seconds = round(time.time() - started, 1)
    return result
