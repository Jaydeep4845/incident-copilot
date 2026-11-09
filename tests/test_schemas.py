import pytest
from pydantic import ValidationError

from agent.schemas import Evidence, EvidenceKind, RCAReport, Remediation, RemediationAction

EVIDENCE = [
    Evidence(kind=EvidenceKind.METRIC, query="pg_stat_activity_count", finding="pool at 100%"),
    Evidence(kind=EvidenceKind.LOG, query='{service="orders"} |= "timeout"', finding="timeouts"),
]


def test_report_with_two_pieces_of_evidence_is_valid():
    report = RCAReport(
        alert_name="HighLatencyOrders",
        affected_service="orders",
        root_cause="Postgres connection pool exhausted",
        evidence=EVIDENCE,
        confidence=0.7,
        remediation=Remediation(
            action=RemediationAction.RESTART_SERVICE, target="orders", rationale="clears pool"
        ),
    )
    assert report.remediation.requires_approval


def test_single_piece_of_evidence_is_not_enough():
    with pytest.raises(ValidationError):
        RCAReport(
            alert_name="a",
            affected_service="b",
            root_cause="c",
            evidence=EVIDENCE[:1],
            confidence=0.5,
            remediation=Remediation(action=RemediationAction.NONE, rationale="n/a"),
        )


def test_remediation_cannot_skip_human_approval():
    with pytest.raises(ValidationError):
        Remediation(
            action=RemediationAction.ROLLBACK_DEPLOY,
            target="orders",
            rationale="x",
            requires_approval=False,
        )
