"""Contract for the agent's root-cause report.

The report must name the cause from a fixed vocabulary (so it can be scored objectively) and
point at concrete evidence returned by the investigation tools, so a reviewer can check the
reasoning instead of trusting it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class CauseCategory(StrEnum):
    BAD_DEPLOY_CODE = "bad_deploy_code"
    BAD_DEPLOY_CONFIG = "bad_deploy_config"
    DB_MISSING_INDEX = "db_missing_index"
    DB_LOCK_CONTENTION = "db_lock_contention"
    DEPENDENCY_DOWN = "dependency_down"
    SLOW_DEPENDENCY = "slow_dependency"
    RESOURCE_EXHAUSTION = "resource_exhaustion"


class Component(StrEnum):
    GATEWAY = "gateway"
    ORDERS = "orders"
    INVENTORY = "inventory"
    PAYMENTS = "payments"
    POSTGRES = "postgres"
    REDIS = "redis"
    PAYMENT_PROVIDER = "payment-provider"


class Evidence(BaseModel):
    id: str = Field(description="Evidence id returned by a tool, e.g. M3, L2, T1, D1, R1")
    finding: str = Field(description="What this evidence shows, in one sentence")


class RemediationAction(StrEnum):
    """Only these actions may ever be proposed; each still needs human approval."""

    ROLLBACK_DEPLOY = "rollback_deploy"
    RESTART_SERVICE = "restart_service"
    ESCALATE = "escalate"
    NONE = "none"


class Remediation(BaseModel):
    action: RemediationAction
    target: str | None = None
    rationale: str
    requires_approval: bool = True

    @model_validator(mode="after")
    def target_required_for_actions(self) -> Remediation:
        if self.action in (RemediationAction.ROLLBACK_DEPLOY, RemediationAction.RESTART_SERVICE):
            if not self.target:
                raise ValueError("a target service is required for rollback or restart")
        if not self.requires_approval:
            raise ValueError("remediation must always require human approval")
        return self


class RCAReport(BaseModel):
    cause_category: CauseCategory
    component: Component = Field(
        description="Where the root cause is (not just where symptoms show)"
    )
    root_cause: str = Field(description="One or two sentences: what happened and why")
    evidence: list[Evidence] = Field(min_length=2)
    confidence: float = Field(ge=0, le=1)
    remediation: Remediation

    def unknown_evidence(self, known_ids: set[str]) -> list[str]:
        """Evidence ids the tools never returned (fabricated or mistyped citations)."""
        return [e.id for e in self.evidence if e.id not in known_ids]
