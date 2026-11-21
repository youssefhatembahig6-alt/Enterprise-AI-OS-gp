"""M2 report contracts: tenant report profile, report intent, run states, event payloads
and verification outcomes. Documented in docs/contracts/reports.md.

Everything here is data. Nothing in this module trusts model output.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

EVENT_VERSION = 1

# Output columns every supported sales query must return, in this order.
OUTPUT_COLUMNS: tuple[str, ...] = ("order_count", "net_sales", "tax_amount", "gross_sales")
COLUMN_LABELS: dict[str, str] = {
    "order_count": "Fulfilled orders",
    "net_sales": "Net sales (tax-exclusive)",
    "tax_amount": "Tax",
    "gross_sales": "Gross sales (tax-inclusive)",
}


class ReportProfile(BaseModel):
    """Tenant configuration of the supported report (``tenant_report_profiles``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_key: str
    resource_id: UUID
    version: int
    title: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    date_basis: Literal["order_date"]
    included_statuses: tuple[str, ...]
    status_domain: tuple[str, ...]
    net_measure: Literal["subtotal"]
    tax_measure: Literal["tax"]
    gross_measure: Literal["total"]
    tax_treatment: str
    periods: tuple[Literal["quarter", "month"], ...]
    fiscal_year_start_month: Literal[1]

    @field_validator("included_statuses")
    @classmethod
    def _non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("included_statuses must not be empty")
        return value

    @classmethod
    def from_row(
        cls, report_key: str, resource_id: UUID, version: int, config: dict[str, Any]
    ) -> ReportProfile:
        return cls(report_key=report_key, resource_id=resource_id, version=version, **config)


class Period(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["quarter", "month"]
    start: date  # inclusive
    end: date  # inclusive
    label: str


class SalesReportIntent(BaseModel):
    """Trusted, rule-derived meaning of the request. The model never sets these fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_key: str
    period: Period
    date_basis: str
    included_statuses: tuple[str, ...]
    currency: str
    tax_basis: Literal["net_is_tax_exclusive"] = "net_is_tax_exclusive"
    grain: Literal["period_total"] = "period_total"
    columns: tuple[str, ...] = OUTPUT_COLUMNS

    def describe(self) -> dict[str, Any]:
        return {
            "report_key": self.report_key,
            "period": self.period.model_dump(mode="json"),
            "date_basis": self.date_basis,
            "included_statuses": list(self.included_statuses),
            "currency": self.currency,
            "tax_basis": self.tax_basis,
            "grain": self.grain,
            "columns": list(self.columns),
        }


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PUBLISHED = "published"
    ABSTAINED = "abstained"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {RunStatus.PUBLISHED, RunStatus.ABSTAINED, RunStatus.FAILED, RunStatus.CANCELLED}
)


class EventType(StrEnum):
    """The six SSE events of the primary specification (section 7, Streaming)."""

    STATUS = "status"
    TOKEN = "token"  # noqa: S105 - event name, not a credential
    CITATION = "citation"
    APPROVAL_REQUIRED = "approval_required"  # reserved; never emitted in M2
    ERROR = "error"
    DONE = "done"


# Events that carry verified content. They are written only inside the publication
# transaction and are replayed only after a fresh authorization check.
CONTENT_EVENTS = frozenset({EventType.TOKEN, EventType.CITATION})


class Step(StrEnum):
    ACCEPTED = "accepted"
    AUTHORIZATION = "authorization"
    ROUTING = "routing"
    SQL_GENERATION = "sql_generation"
    SQL_VALIDATION = "sql_validation"
    QUERY_EXECUTION = "query_execution"
    QUERY_VERIFICATION = "query_verification"
    REPORT_DRAFTING = "report_drafting"
    REPORT_VERIFICATION = "report_verification"
    RETRIEVAL = "retrieval"
    ANSWER_DRAFTING = "answer_drafting"
    ANSWER_VERIFICATION = "answer_verification"
    PUBLICATION = "publication"


STEP_LABELS: dict[Step, str] = {
    Step.ACCEPTED: "Request accepted",
    Step.AUTHORIZATION: "Checked your access",
    Step.ROUTING: "Understood the request",
    Step.SQL_GENERATION: "Drafted a read-only query",
    Step.SQL_VALIDATION: "Validated the query",
    Step.QUERY_EXECUTION: "Ran a read-only query",
    Step.QUERY_VERIFICATION: "Checked the query's meaning",
    Step.REPORT_DRAFTING: "Drafted the report",
    Step.REPORT_VERIFICATION: "Verified figures and claims",
    Step.RETRIEVAL: "Searched documents you can access",
    Step.ANSWER_DRAFTING: "Drafted the answer",
    Step.ANSWER_VERIFICATION: "Checked the answer against its sources",
    Step.PUBLICATION: "Published the verified result",
}


class Outcome(StrEnum):
    PUBLISHED = "published"
    ABSTAINED = "abstained"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AbstentionCode(StrEnum):
    # One public code for "not authorized" and "no rows": the browser cannot tell
    # whether data exists that the user may not see. The private reason is audited.
    NO_ACCESSIBLE_DATA = "no_accessible_data"
    UNSUPPORTED_REQUEST = "unsupported_request"
    CLARIFICATION_NEEDED = "clarification_needed"
    PERIOD_UNAVAILABLE = "period_unavailable"
    VERIFICATION_FAILED = "verification_failed"
    # M3: the single no-sources response. Identical whether nothing relevant exists or
    # the user may not see it (FR-04, T10); the private reason is audited.
    NO_SOURCES = "no_sources"


ABSTENTION_MESSAGES: dict[AbstentionCode, str] = {
    AbstentionCode.NO_ACCESSIBLE_DATA: (
        "No data you are authorized to access answers this question."
    ),
    AbstentionCode.UNSUPPORTED_REQUEST: (
        "This request is outside the supported report. I can report fulfilled sales "
        "(order count, net, tax and gross) for one calendar quarter or month."
    ),
    AbstentionCode.CLARIFICATION_NEEDED: (
        "Please name one calendar quarter or month, for example “Q1 2026” or “February 2026”."
    ),
    AbstentionCode.PERIOD_UNAVAILABLE: (
        "The requested period is not fully covered by the available data."
    ),
    AbstentionCode.NO_SOURCES: (
        "I couldn't find information in documents you can access that answers this."
    ),
    AbstentionCode.VERIFICATION_FAILED: (
        "I could not produce a result that passed verification, so no answer is shown."
    ),
}


class ErrorCode(StrEnum):
    MODEL_UNAVAILABLE = "model_unavailable"
    REPORTS_UNAVAILABLE = "reports_unavailable"
    ACCESS_REVOKED = "access_revoked"
    CANCELLED = "cancelled"
    INTERNAL = "internal_error"
    # M4: the tenant switched off the pack this run needs (checked at start and at
    # publication, so a run started before the change publishes nothing).
    CAPABILITY_DISABLED = "capability_disabled"


ERROR_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.MODEL_UNAVAILABLE: "The model service is unavailable. Try again later.",
    ErrorCode.REPORTS_UNAVAILABLE: "Reporting is not configured on this server.",
    ErrorCode.ACCESS_REVOKED: "Your access changed during this request, so nothing is shown.",
    ErrorCode.CANCELLED: "The request was cancelled.",
    ErrorCode.INTERNAL: "The request failed.",
    ErrorCode.CAPABILITY_DISABLED: "This capability is not enabled for your organization.",
}


class VerificationLabel(StrEnum):
    # Figures verified deterministically AND narrative sentences scored by the
    # development NLI checker (public baseline, not the M7 trained checker).
    VERIFIED_DEV_CHECKER = "figures_verified_narrative_dev_checked"
    # Figures verified deterministically; no model narrative is shown.
    STRUCTURED_ONLY = "figures_verified_structured_only"
    # M3 document answers: every sentence quotes an exact span of a cited, currently
    # authorized document version, and passed the development NLI checker.
    ANSWER_VERIFIED_DEV_CHECKER = "answer_quotes_verified_dev_checked"


class EvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: UUID
    row: int = Field(ge=0)
    column: str
