import pytest
from pydantic import ValidationError

from agent.schemas import (
    CauseCategory,
    Component,
    Evidence,
    RCAReport,
    Remediation,
    RemediationAction,
)

EVIDENCE = [
    Evidence(id="M1", finding="orders DB pool at 2/2 connections with 30 requests waiting"),
    Evidence(id="D1", finding="orders 1.9.0 lowered DB_POOL_SIZE one minute before the alert"),
]


def report(**kw) -> RCAReport:
    fields = dict(
        cause_category=CauseCategory.BAD_DEPLOY_CONFIG,
        component=Component.ORDERS,
        root_cause="orders 1.9.0 reduced the DB pool to 2 connections",
        evidence=EVIDENCE,
        confidence=0.8,
        remediation=Remediation(
            action=RemediationAction.ROLLBACK_DEPLOY, target="orders", rationale="restore pool size"
        ),
    )
    fields.update(kw)
    return RCAReport(**fields)


def test_report_with_two_pieces_of_evidence_is_valid():
    assert report().remediation.requires_approval


def test_single_piece_of_evidence_is_not_enough():
    with pytest.raises(ValidationError):
        report(evidence=EVIDENCE[:1])


def test_cause_must_come_from_the_fixed_vocabulary():
    with pytest.raises(ValidationError):
        report(cause_category="cosmic_rays")


def test_remediation_cannot_skip_human_approval():
    with pytest.raises(ValidationError):
        Remediation(
            action=RemediationAction.ROLLBACK_DEPLOY,
            target="orders",
            rationale="x",
            requires_approval=False,
        )


def test_rollback_needs_a_target():
    with pytest.raises(ValidationError):
        Remediation(action=RemediationAction.ROLLBACK_DEPLOY, rationale="x")


def test_fabricated_evidence_ids_are_detected():
    assert report().unknown_evidence({"M1"}) == ["D1"]
