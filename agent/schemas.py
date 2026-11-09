"""Contract for the agent's root-cause report.

The report must point at concrete evidence, so a reviewer can check the agent's reasoning
instead of trusting it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class EvidenceKind(StrEnum):
    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    DEPLOY = "deploy"
    RUNBOOK = "runbook"


class Evidence(BaseModel):
    kind: EvidenceKind
    query: str = Field(description="PromQL, LogQL, trace id, commit sha or runbook path used")
    finding: str
    observed_at: datetime | None = None


class RemediationAction(StrEnum):
    """Only these actions may ever be proposed; each still needs human approval."""

    RESTART_SERVICE = "restart_service"
    ROLLBACK_DEPLOY = "rollback_deploy"
    SCALE_WORKERS = "scale_workers"
    NONE = "none"


class Remediation(BaseModel):
    action: RemediationAction
    target: str | None = None
    rationale: str
    requires_approval: bool = True

    @model_validator(mode="after")
    def target_required_for_actions(self) -> Remediation:
        if self.action is not RemediationAction.NONE and not self.target:
            raise ValueError("a target service is required for any remediation action")
        if not self.requires_approval:
            raise ValueError("remediation must always require human approval")
        return self


class RCAReport(BaseModel):
    alert_name: str
    affected_service: str
    root_cause: str
    evidence: list[Evidence] = Field(min_length=2)
    confidence: float = Field(ge=0, le=1)
    remediation: Remediation
    investigation_seconds: float | None = Field(default=None, ge=0)
    llm_cost_usd: float | None = Field(default=None, ge=0)
