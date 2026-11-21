"""LangGraph report workflow: router -> SQL -> execution -> report -> verification.

    prepare -> route -> generate_sql -> check_sql -> execute -> persist_evidence
            -> draft_report -> verify_report -> publish -> finish

* The graph is plain code (the orchestrator); the model only drafts router labels,
  SQL and narrative sentences. Access context comes from PostgreSQL, never the model.
* Before verification only ``status`` events are written. Content (``token`` and
  ``citation``) is written only inside the publication transaction.
* SQL drafts get at most ``REPORT_MAX_REPAIRS`` (<= 2) repairs after the first draft;
  so do narrative drafts. Exhausted SQL repairs abstain. A narrative that cannot be
  verified is withheld and only the deterministically verified table is published,
  labelled accordingly. Rejected drafts are never stored in events or results.
* Authorization (``dataset.query``) is re-evaluated at start, before execution, when
  persisting evidence and inside the publication transaction, which holds a share
  lock on the tenant policy epoch (ADR 0203).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy import Engine, text

from app.config import ModelMode, Settings
from app.db.engine import tenant_transaction
from app.reporting import store
from app.reporting.access import authorize_dataset
from app.reporting.claims import check_sentences
from app.reporting.contracts import (
    ABSTENTION_MESSAGES,
    COLUMN_LABELS,
    ERROR_MESSAGES,
    OUTPUT_COLUMNS,
    STEP_LABELS,
    TERMINAL_STATUSES,
    AbstentionCode,
    ErrorCode,
    EventType,
    Outcome,
    ReportProfile,
    RunStatus,
    SalesReportIntent,
    Step,
    VerificationLabel,
)
from app.reporting.executor import QueryError, QueryResult, ReportExecutor
from app.reporting.model_client import (
    FAKE_LABEL,
    CancelToken,
    InferenceCancelled,
    ModelClient,
    ModelError,
)
from app.reporting.prompts import (
    PROMPT_VERSION,
    MalformedOutput,
    format_money,
    parse_route,
    parse_sentences,
    parse_sql,
    premise,
    report_messages,
    router_messages,
    sql_messages,
)
from app.reporting.router import route
from app.reporting.semantics import QuerySemantics, SemanticVerifier, check_probe
from app.reporting.sql_guard import SqlRejected, ValidatedSql, validate_sql
from app.repositories.audit import record_audit_event
from app.tenancy.config import pack_enabled

logger = logging.getLogger("eaios.reports")

SUPPORTED_REPORT = "sales.fulfilled"
SQL_MAX_TOKENS = 400
REPORT_MAX_TOKENS = 400
ROUTER_MAX_TOKENS = 20

Terminal = dict[str, Any]


class RunState(TypedDict, total=False):
    run_id: UUID
    tenant_id: UUID
    user_id: UUID
    question: str
    inference_mode: str
    profile: ReportProfile
    intent: SalesReportIntent
    dataset: dict[str, Any]
    sql_attempt: int
    sql_feedback: list[str]
    model_sql: str
    validated: ValidatedSql
    semantics: QuerySemantics
    result: QueryResult
    probe: dict[str, Any]
    row: dict[str, Any]
    evidence_id: UUID
    evidence_sha256: str
    report_attempt: int
    report_feedback: list[str]
    sentences: list[dict[str, Any]]
    narrative_status: str
    checker_scores: list[float]
    terminal: Terminal | None
    stage: str


class _Cancelled(Exception):
    pass


@dataclass
class WorkflowDeps:
    app_engine: Engine
    executor: ReportExecutor | None
    model: ModelClient
    settings: Settings
    # Test hook: called with the node name before each node runs.
    before_node: Callable[[str, RunState], None] | None = None


def _scale(value: Any) -> int:
    exponent = Decimal(str(value)).as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _money_display(currency: str, value: Any) -> str:
    return f"{currency} {format_money(str(value))}"


class ReportWorkflow:
    def __init__(self, deps: WorkflowDeps) -> None:
        self.deps = deps
        self.max_drafts = 1 + deps.settings.report_max_repairs
        self._cancel_tokens: dict[UUID, CancelToken] = {}
        self._stats: dict[UUID, dict[str, Any]] = {}
        self.graph = self._build()

    # -- graph ---------------------------------------------------------------------

    def _build(self) -> Any:
        graph: StateGraph[RunState] = StateGraph(RunState)
        nodes: dict[str, Callable[[RunState], dict[str, Any]]] = {
            "prepare": self.prepare,
            "route": self.route,
            "generate_sql": self.generate_sql,
            "check_sql": self.check_sql,
            "execute": self.execute,
            "persist_evidence": self.persist_evidence,
            "draft_report": self.draft_report,
            "verify_report": self.verify_report,
            "publish": self.publish,
            "finish": self.finish,
        }
        for name, fn in nodes.items():
            graph.add_node(name, self._wrap(name, fn))  # type: ignore[call-overload]
        graph.add_edge(START, "prepare")
        graph.add_conditional_edges("prepare", self._next_or_finish("route"))
        graph.add_conditional_edges("route", self._next_or_finish("generate_sql"))
        graph.add_conditional_edges("generate_sql", self._after_sql_draft)
        graph.add_conditional_edges("check_sql", self._after_sql_check)
        graph.add_conditional_edges("execute", self._after_execute)
        graph.add_conditional_edges("persist_evidence", self._next_or_finish("draft_report"))
        graph.add_conditional_edges("draft_report", self._after_report_draft)
        graph.add_conditional_edges("verify_report", self._after_report_check)
        graph.add_edge("publish", "finish")
        graph.add_edge("finish", END)
        return graph.compile()

    @staticmethod
    def _next_or_finish(nxt: str) -> Callable[[RunState], str]:
        def choose(state: RunState) -> str:
            return "finish" if state.get("terminal") else nxt

        return choose

    def _sql_retry(self, state: RunState) -> str:
        if state.get("terminal"):
            return "finish"
        if state.get("sql_feedback"):
            return "generate_sql" if state.get("sql_attempt", 0) < self.max_drafts else "finish"
        return ""

    def _after_sql_draft(self, state: RunState) -> str:
        return self._sql_retry(state) or "check_sql"

    def _after_sql_check(self, state: RunState) -> str:
        return self._sql_retry(state) or "execute"

    def _after_execute(self, state: RunState) -> str:
        return self._sql_retry(state) or "persist_evidence"

    def _after_report_draft(self, state: RunState) -> str:
        if state.get("terminal"):
            return "finish"
        if state.get("narrative_status"):  # withheld (model or checker unavailable)
            return "publish"
        if state.get("report_feedback"):
            return "draft_report" if state.get("report_attempt", 0) < self.max_drafts else "publish"
        return "verify_report"

    def _after_report_check(self, state: RunState) -> str:
        if state.get("terminal"):
            return "finish"
        if state.get("report_feedback") and not state.get("narrative_status"):
            return "draft_report" if state.get("report_attempt", 0) < self.max_drafts else "publish"
        return "publish"

    def _wrap(
        self, name: str, fn: Callable[[Any], dict[str, Any]]
    ) -> Callable[[Any], dict[str, Any]]:
        def node(state: RunState) -> dict[str, Any]:
            if self.deps.before_node is not None:
                self.deps.before_node(name, state)
            if name != "finish" and state.get("terminal"):
                return {}
            started = time.monotonic()
            try:
                if name not in ("finish",):
                    self._check_cancel(state)
                update = fn(state)
            except (_Cancelled, InferenceCancelled):
                update = {"terminal": {"kind": "cancel", "code": ErrorCode.CANCELLED}}
            except ModelError as exc:
                logger.warning("model_error", extra={"stage": name, "code": exc.code})
                self._stat(state, "model_errors", f"{name}:{exc.code}")
                update = {
                    "terminal": {
                        "kind": "fail",
                        "code": ErrorCode.MODEL_UNAVAILABLE,
                        "private": exc.code,
                    }
                }
            except Exception:
                logger.exception("report_node_failed", extra={"stage": name})
                update = {"terminal": {"kind": "fail", "code": ErrorCode.INTERNAL}}
            self._timing(state, name, time.monotonic() - started)
            return update

        # LangGraph derives a node's input schema from its first parameter's annotation.
        # Drop it so every node receives the graph's own state schema (RunState here,
        # the document workflow's DocState in its subclass).
        node.__annotations__.pop("state", None)
        return node

    # -- helpers -------------------------------------------------------------------

    def _tx(self, state: Any) -> Any:
        return tenant_transaction(self.deps.app_engine, state["tenant_id"], state["user_id"])

    def _stats_for(self, state: Any) -> dict[str, Any]:
        return self._stats.setdefault(state["run_id"], {"timings": {}, "rejections": []})

    def _stat(self, state: Any, key: str, value: Any) -> None:
        self._stats_for(state).setdefault(key, []).append(value)

    def _timing(self, state: Any, name: str, seconds: float) -> None:
        timings = self._stats_for(state)["timings"]
        timings[name] = round(timings.get(name, 0.0) + seconds, 3)

    def _check_cancel(self, state: Any) -> None:
        token = self._cancel_tokens.get(state["run_id"])
        if token is not None and token.cancelled:
            raise _Cancelled
        with self._tx(state) as conn:
            if store.cancel_requested(conn, state["run_id"]):
                if token is not None:
                    token.cancel()
                raise _Cancelled

    def _status(self, state: Any, step: Step, phase: str, **extra: Any) -> None:
        payload: dict[str, Any] = {
            "step": step.value,
            "state": phase,
            "label": STEP_LABELS[step],
            "inference": state.get("inference_mode", self.deps.model.mode.value),
            **extra,
        }
        if payload["inference"] == ModelMode.FAKE.value:
            payload["inference_label"] = FAKE_LABEL
        with self._tx(state) as conn:
            store.append_event(conn, state["run_id"], state["tenant_id"], EventType.STATUS, payload)

    def _audit(self, conn: Any, state: Any, **fields: Any) -> None:
        record_audit_event(
            conn,
            request_id=None,
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            **fields,
        )

    # -- nodes ---------------------------------------------------------------------

    def prepare(self, state: RunState) -> dict[str, Any]:
        with self._tx(state) as conn:
            run = store.load_run(conn, state["run_id"])
            if run is None or run.status in TERMINAL_STATUSES:
                return {"terminal": {"kind": "noop"}}
            store.update_run(conn, run.id, status=RunStatus.RUNNING)
            if not pack_enabled(conn, "report"):
                return {"terminal": self._disabled("start")}
            profile = store.load_profile(conn, SUPPORTED_REPORT)
            if profile is None:
                return {"terminal": self._abstain(AbstentionCode.NO_ACCESSIBLE_DATA, "no_profile")}
            store.update_run(
                conn, run.id, resource_id=profile.resource_id, report_key=profile.report_key
            )
            decision = authorize_dataset(conn, state["user_id"], profile.resource_id)
            self._audit(
                conn,
                state,
                event_type="report.authorize",
                action="dataset.query",
                resource_id=profile.resource_id,
                decision="allow" if decision.allowed else "deny",
                reason=decision.reason.value,
                details={"run_id": str(run.id), "stage": "start"},
            )
            dataset = store.dataset_info(conn)
        self._status(state, Step.AUTHORIZATION, "completed")
        if not decision.allowed:
            return {
                "terminal": self._abstain(AbstentionCode.NO_ACCESSIBLE_DATA, decision.reason.value)
            }
        if dataset is None:
            return {"terminal": self._abstain(AbstentionCode.NO_ACCESSIBLE_DATA, "no_dataset")}
        stats = self._stats_for(state)
        stats["model"] = self.deps.model.describe()
        stats["prompt_version"] = PROMPT_VERSION
        stats["profile_version"] = profile.version
        return {"profile": profile, "dataset": dataset, "sql_attempt": 0, "report_attempt": 0}

    def route(self, state: RunState) -> dict[str, Any]:
        profile = state["profile"]
        decision = route(state["question"], profile)
        self._stats_for(state)["route"] = {"kind": decision.kind, "reason": decision.reason}
        if decision.kind == "clarify":
            return {"terminal": self._abstain(AbstentionCode.CLARIFICATION_NEEDED, decision.reason)}
        if decision.kind == "unsupported":
            return {"terminal": self._abstain(AbstentionCode.UNSUPPORTED_REQUEST, decision.reason)}
        if decision.intent is None:
            return {"terminal": self._abstain(AbstentionCode.CLARIFICATION_NEEDED, "no_intent")}
        if decision.kind == "ask_model":
            if self.deps.model.mode is ModelMode.GATEWAY:
                self._stats_for(state)["served_models"] = self.deps.model.served_models(
                    self._token(state)
                ).summary()
            answer = self.deps.model.chat(
                router_messages(state["question"]),
                purpose="router",
                max_tokens=ROUTER_MAX_TOKENS,
                cancel=self._token(state),
            )
            try:
                label = parse_route(answer.text)
            except MalformedOutput as exc:
                return {"terminal": self._abstain(AbstentionCode.UNSUPPORTED_REQUEST, exc.code)}
            if label != "sales_report":
                return {
                    "terminal": self._abstain(AbstentionCode.UNSUPPORTED_REQUEST, "model_router")
                }
        intent = decision.intent
        reference: date = state["dataset"]["reference_date"]
        if intent.period.end > reference:
            return {
                "terminal": self._abstain(AbstentionCode.PERIOD_UNAVAILABLE, "after_reference_date")
            }
        self._status(state, Step.ROUTING, "completed", period=intent.period.label)
        return {"intent": intent}

    def _token(self, state: Any) -> CancelToken:
        return self._cancel_tokens.setdefault(state["run_id"], CancelToken())

    def _repair(self, state: RunState, stage: str, codes: list[str]) -> dict[str, Any]:
        for code in codes:
            self._stat(state, "rejections", f"{stage}:{code}")
        attempt = int(
            state.get("sql_attempt", 0) if stage == "sql" else state.get("report_attempt", 0)
        )
        key = "sql_feedback" if stage == "sql" else "report_feedback"
        if attempt >= self.max_drafts and stage == "sql":
            return {
                key: codes,
                "terminal": self._abstain(
                    AbstentionCode.VERIFICATION_FAILED, f"sql_repairs_exhausted:{codes[0]}"
                ),
            }
        return {key: codes}

    def generate_sql(self, state: RunState) -> dict[str, Any]:
        attempt = state.get("sql_attempt", 0) + 1
        if attempt > 1:
            self._status(
                state,
                Step.SQL_GENERATION,
                "retrying",
                attempt=attempt,
                max_attempts=self.max_drafts,
            )
        elif self.deps.model.mode is ModelMode.GATEWAY and "served_models" not in self._stats_for(
            state
        ):
            self._stats_for(state)["served_models"] = self.deps.model.served_models(
                self._token(state)
            ).summary()
        answer = self.deps.model.chat(
            sql_messages(state["intent"], state["profile"], state.get("sql_feedback")),
            purpose="sql",
            max_tokens=SQL_MAX_TOKENS,
            cancel=self._token(state),
        )
        self._stat(
            state,
            "model_calls",
            {
                "purpose": "sql",
                "seconds": round(answer.total_seconds, 3),
                "ttft": answer.ttft_seconds,
                "tokens": answer.completion_tokens,
            },
        )
        update: dict[str, Any] = {"sql_attempt": attempt}
        try:
            if answer.finish_reason == "length":
                raise MalformedOutput("truncated_output")
            update["model_sql"] = parse_sql(answer.text)
            update["sql_feedback"] = []
        except MalformedOutput as exc:
            retry_state: RunState = {**state, "sql_attempt": attempt}
            update.update(self._repair(retry_state, "sql", [f"malformed_output_{exc.code}"]))
            return update
        self._status(state, Step.SQL_GENERATION, "completed", attempt=attempt)
        return update

    def check_sql(self, state: RunState) -> dict[str, Any]:
        try:
            validated = validate_sql(state["model_sql"])
        except SqlRejected as exc:
            return self._repair(state, "sql", [f"sql_{exc.code}"])
        semantics = SemanticVerifier(state["profile"], state["intent"]).verify(validated)
        if not semantics.ok:
            return self._repair(state, "sql", semantics.failures)
        self._status(state, Step.SQL_VALIDATION, "completed")
        return {"validated": validated, "semantics": semantics, "sql_feedback": []}

    def execute(self, state: RunState) -> dict[str, Any]:
        executor = self.deps.executor
        if executor is None:
            return {"terminal": {"kind": "fail", "code": ErrorCode.REPORTS_UNAVAILABLE}}
        with self._tx(state) as conn:
            decision = authorize_dataset(conn, state["user_id"], state["profile"].resource_id)
        if not decision.allowed:
            return {"terminal": self._revoked(decision.reason.value, "before_execution")}
        semantics = state["semantics"]
        if semantics.probe_sql is None:
            return self._repair(state, "sql", ["unverifiable_structure"])
        try:
            probe = executor.execute(
                tenant_id=state["tenant_id"],
                user_id=state["user_id"],
                run_id=state["run_id"],
                sql=semantics.probe_sql,
            )
            if len(probe.rows) != 1:
                return self._repair(state, "sql", ["unverifiable_structure"])
            probe_row = probe.row_dicts()[0]
            check_probe(probe_row, state["profile"], semantics)
            if not semantics.ok:
                return self._repair(state, "sql", semantics.failures)
            result = executor.execute(
                tenant_id=state["tenant_id"],
                user_id=state["user_id"],
                run_id=state["run_id"],
                sql=state["validated"].deparsed,
            )
        except QueryError as exc:
            return self._repair(state, "sql", [f"query_{exc.code}"])
        self._status(state, Step.QUERY_EXECUTION, "completed")
        names = [c["name"] for c in result.columns]
        if len(result.rows) != 1 or sorted(names) != sorted(OUTPUT_COLUMNS):
            return self._repair(state, "sql", ["wrong_output_columns"])
        row = result.row_dicts()[0]
        types = {c["name"]: c["type"] for c in result.columns}
        if types["order_count"] != "integer" or any(
            types[c] != "decimal" for c in OUTPUT_COLUMNS if c != "order_count"
        ):
            return self._repair(state, "sql", ["wrong_output_types"])
        if row["order_count"] == 0:
            return {
                "terminal": self._abstain(AbstentionCode.NO_ACCESSIBLE_DATA, "no_matching_rows")
            }
        if row["order_count"] != int(probe_row["distinct_orders"]):
            return self._repair(state, "sql", ["join_fan_out"])
        if any(row[c] is None for c in OUTPUT_COLUMNS):
            return self._repair(state, "sql", ["null_aggregate"])
        net, tax, gross = (Decimal(row[c]) for c in ("net_sales", "tax_amount", "gross_sales"))
        if net + tax != gross:
            return {
                "terminal": self._abstain(
                    AbstentionCode.VERIFICATION_FAILED, "arithmetic_inconsistent"
                )
            }
        # Sums of numeric(14,2) amounts keep exactly two decimals. Any other scale means
        # the value was cast or rounded somewhere, so it cannot be presented as exact.
        if any(_scale(row[c]) != 2 for c in ("net_sales", "tax_amount", "gross_sales")):
            return self._repair(state, "sql", ["value_changing_cast"])
        self._status(state, Step.QUERY_VERIFICATION, "completed")
        return {"result": result, "row": row, "probe": probe_row, "sql_feedback": []}

    def persist_evidence(self, state: RunState) -> dict[str, Any]:
        profile, dataset = state["profile"], state["dataset"]
        with self._tx(state) as conn:
            decision = authorize_dataset(conn, state["user_id"], profile.resource_id)
            if not decision.allowed:
                return {"terminal": self._revoked(decision.reason.value, "evidence")}
            epoch = conn.execute(text("SELECT policy_epoch FROM tenants")).scalar_one()
            semantics = {
                "report": state["intent"].describe(),
                "profile_version": profile.version,
                "verified": state["semantics"].as_record(),
                "join_probe": state["probe"],
            }
            evidence_id, digest = store.insert_evidence(
                conn,
                tenant_id=state["tenant_id"],
                run_id=state["run_id"],
                owner_user_id=state["user_id"],
                resource_id=profile.resource_id,
                report_key=profile.report_key,
                model_sql=state["validated"].original,
                result=state["result"],
                semantics=semantics,
                dataset_fingerprint=f"{dataset['dataset']}:{dataset['fingerprint']}",
                policy_epoch=int(epoch),
            )
            self._audit(
                conn,
                state,
                event_type="report.query",
                action="dataset.query",
                resource_id=profile.resource_id,
                decision="allow",
                reason="query_executed",
                details={
                    "run_id": str(state["run_id"]),
                    "evidence_id": str(evidence_id),
                    "row_count": len(state["result"].rows),
                    "policy_epoch": int(epoch),
                },
            )
        return {"evidence_id": evidence_id, "evidence_sha256": digest}

    def draft_report(self, state: RunState) -> dict[str, Any]:
        attempt = state.get("report_attempt", 0) + 1
        if attempt > 1:
            self._status(
                state,
                Step.REPORT_DRAFTING,
                "retrying",
                attempt=attempt,
                max_attempts=self.max_drafts,
            )
        try:
            answer = self.deps.model.chat(
                report_messages(
                    state["intent"], state["profile"], state["row"], state.get("report_feedback")
                ),
                purpose="report",
                max_tokens=REPORT_MAX_TOKENS,
                cancel=self._token(state),
            )
        except InferenceCancelled:
            raise
        except ModelError as exc:
            self._stat(state, "model_errors", f"report:{exc.code}")
            return {"report_attempt": attempt, "narrative_status": "withheld_model_unavailable"}
        self._stat(
            state,
            "model_calls",
            {
                "purpose": "report",
                "seconds": round(answer.total_seconds, 3),
                "ttft": answer.ttft_seconds,
                "tokens": answer.completion_tokens,
            },
        )
        try:
            if answer.finish_reason == "length":
                raise MalformedOutput("truncated_output")
            sentences = parse_sentences(answer.text)
        except MalformedOutput as exc:
            self._stat(state, "rejections", f"report:malformed_output_{exc.code}")
            return {
                "report_attempt": attempt,
                "report_feedback": [f"malformed output ({exc.code})"],
                "sentences": [],
            }
        self._status(state, Step.REPORT_DRAFTING, "completed", attempt=attempt)
        return {"report_attempt": attempt, "sentences": sentences, "report_feedback": []}

    def verify_report(self, state: RunState) -> dict[str, Any]:
        sentences, row, intent = state["sentences"], state["row"], state["intent"]
        feedback: list[str] = []
        for check in check_sentences(sentences, row, intent):
            for code in check.failures:
                self._stat(state, "rejections", f"report:{code}")
                feedback.append(f"sentence {check.index + 1}: {code}")
        if feedback:
            return {"report_feedback": feedback}
        evidence_text = premise(intent, state["profile"], row)
        try:
            scores = self.deps.model.check(
                [(evidence_text, s["text"]) for s in sentences], cancel=self._token(state)
            )
        except InferenceCancelled:
            raise
        except ModelError as exc:
            self._stat(state, "model_errors", f"check:{exc.code}")
            return {"narrative_status": "withheld_checker_unavailable"}
        threshold = self.deps.settings.checker_entailment_threshold
        values = [round(s.entailment, 4) for s in scores]
        self._stats_for(state)["checker_scores"] = values
        unsupported = [i for i, score in enumerate(scores) if score.entailment < threshold]
        if unsupported:
            for _ in unsupported:
                self._stat(state, "rejections", "report:claim_not_supported")
            return {
                "report_feedback": [
                    f"sentence {i + 1}: not supported by the verified figures" for i in unsupported
                ],
                "checker_scores": values,
            }
        self._status(state, Step.REPORT_VERIFICATION, "completed")
        return {"narrative_status": "verified", "checker_scores": values, "report_feedback": []}

    # -- endings -------------------------------------------------------------------

    @staticmethod
    def _abstain(code: AbstentionCode, private: str) -> Terminal:
        return {"kind": "abstain", "code": code, "private": private}

    @staticmethod
    def _revoked(private: str, stage: str) -> Terminal:
        return {"kind": "fail", "code": ErrorCode.ACCESS_REVOKED, "private": f"{stage}:{private}"}

    @staticmethod
    def _disabled(stage: str) -> Terminal:
        return {"kind": "fail", "code": ErrorCode.CAPABILITY_DISABLED, "private": f"{stage}:pack"}

    def _build_result(
        self, state: RunState, message_id: UUID
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        intent, profile, row = state["intent"], state["profile"], state["row"]
        evidence_id = str(state["evidence_id"])
        narrative_status = state.get("narrative_status") or "withheld_failed_verification"
        citations: list[dict[str, Any]] = []
        cite_ids: dict[str, str] = {}
        for index, column in enumerate(OUTPUT_COLUMNS, start=1):
            cite_ids[column] = f"c{index}"
            citations.append(
                {
                    "citation_id": f"c{index}",
                    "evidence_id": evidence_id,
                    "row": 0,
                    "column": column,
                    "label": COLUMN_LABELS[column],
                }
            )
        cells = []
        for column in OUTPUT_COLUMNS:
            value = row[column]
            display = (
                f"{int(value):,}"
                if column == "order_count"
                else _money_display(intent.currency, value)
            )
            cells.append(
                {
                    "column": column,
                    "value": value,
                    "display": display,
                    "ref": {"evidence_id": evidence_id, "row": 0, "column": column},
                    "citation_id": cite_ids[column],
                }
            )
        narrative = []
        if narrative_status == "verified":
            for sentence in state["sentences"]:
                narrative.append(
                    {
                        "text": sentence["text"],
                        "citations": [cite_ids[c] for c in sentence["cites"] if c in cite_ids],
                    }
                )
        label = (
            VerificationLabel.VERIFIED_DEV_CHECKER
            if narrative_status == "verified"
            else VerificationLabel.STRUCTURED_ONLY
        )
        mode = state["inference_mode"]
        verification: dict[str, Any] = {
            "label": label.value,
            "figures": "exact_decimal_match_to_evidence",
            "query_semantics": "verified_against_report_definition",
            "join_multiplicity": "verified_no_fan_out",
            "narrative": narrative_status,
            "inference_mode": mode,
            "checker": None,
        }
        if narrative_status == "verified" and mode == ModelMode.GATEWAY.value:
            verification["checker"] = {
                "model": self.deps.settings.model_checker_name,
                "revision": self.deps.settings.model_checker_revision,
                "status": "development_baseline_not_final_trained_checker",
                "threshold": self.deps.settings.checker_entailment_threshold,
                "threshold_status": "uncalibrated",
                "scores": state.get("checker_scores", []),
            }
        if mode == ModelMode.FAKE.value:
            verification["inference_label"] = FAKE_LABEL
        excluded = [s for s in profile.status_domain if s not in intent.included_statuses]
        result = {
            "message_id": str(message_id),
            "report_key": profile.report_key,
            "title": f"{profile.title}, {intent.period.label}",
            "period": intent.period.model_dump(mode="json"),
            "definition": {
                "date_basis": intent.date_basis,
                "included_statuses": list(intent.included_statuses),
                "excluded_statuses": excluded,
                "currency": intent.currency,
                "tax_treatment": profile.tax_treatment,
                "grain": intent.grain,
            },
            "table": {
                "columns": [
                    {
                        "key": c,
                        "label": COLUMN_LABELS[c],
                        "type": "integer" if c == "order_count" else "money",
                    }
                    for c in OUTPUT_COLUMNS
                ],
                "rows": [cells],
            },
            "narrative": narrative,
            "citations": citations,
            "verification": verification,
            "evidence": {
                "evidence_id": evidence_id,
                "result_sha256": state["evidence_sha256"],
                "dataset_fingerprint": self._fingerprint(state),
            },
        }
        return result, citations

    @staticmethod
    def _fingerprint(state: RunState) -> str:
        return f"{state['dataset']['dataset']}:{state['dataset']['fingerprint']}"

    def publish(self, state: RunState) -> dict[str, Any]:
        message_id = uuid.uuid4()
        result, citations = self._build_result(state, message_id)
        with self._tx(state) as conn:
            # Publication boundary: blocks concurrent policy-epoch bumps until commit.
            epoch = int(conn.execute(text("SELECT lock_tenant_policy_epoch()")).scalar_one())
            run = store.lock_run(conn, state["run_id"])
            if run is None or run.status in TERMINAL_STATUSES:
                return {"terminal": {"kind": "noop"}}
            if run.cancel_requested:
                return {"terminal": {"kind": "cancel", "code": ErrorCode.CANCELLED}}
            if not pack_enabled(conn, "report"):
                return {"terminal": self._disabled("publication")}
            decision = authorize_dataset(conn, state["user_id"], state["profile"].resource_id)
            self._audit(
                conn,
                state,
                event_type="report.publish",
                action="dataset.query",
                resource_id=state["profile"].resource_id,
                decision="allow" if decision.allowed else "deny",
                reason=decision.reason.value,
                details={"run_id": str(run.id), "policy_epoch": epoch},
            )
            if not decision.allowed:
                return {"terminal": self._revoked(decision.reason.value, "publication")}
            rid, tid = state["run_id"], state["tenant_id"]
            store.append_event(
                conn,
                rid,
                tid,
                EventType.STATUS,
                {
                    "step": Step.PUBLICATION.value,
                    "state": "completed",
                    "label": STEP_LABELS[Step.PUBLICATION],
                    "inference": state["inference_mode"],
                    **(
                        {"inference_label": FAKE_LABEL} if state["inference_mode"] == "fake" else {}
                    ),
                },
            )
            store.append_event(
                conn,
                rid,
                tid,
                EventType.TOKEN,
                {
                    "message_id": str(message_id),
                    "part": "table",
                    "title": result["title"],
                    "table": result["table"],
                    "definition": result["definition"],
                },
            )
            for index, sentence in enumerate(result["narrative"]):
                store.append_event(
                    conn,
                    rid,
                    tid,
                    EventType.TOKEN,
                    {
                        "message_id": str(message_id),
                        "part": "text",
                        "index": index,
                        "text": sentence["text"],
                        "citations": sentence["citations"],
                    },
                )
            for citation in citations:
                store.append_event(conn, rid, tid, EventType.CITATION, citation)
            store.append_event(
                conn,
                rid,
                tid,
                EventType.DONE,
                {
                    "message_id": str(message_id),
                    "outcome": Outcome.PUBLISHED.value,
                    "verification": result["verification"],
                    "abstention": None,
                    "inference": state["inference_mode"],
                },
            )
            stats = self._final_stats(state)
            store.update_run(
                conn,
                rid,
                status=RunStatus.PUBLISHED,
                result=result,
                policy_epoch_at_publication=epoch,
                outcome_code=result["verification"]["label"],
                stats=stats,
                finished_at="now",
            )
            store.finish_job(conn, rid)
        return {"terminal": {"kind": "published"}}

    def _final_stats(self, state: Any) -> dict[str, Any]:
        stats = dict(self._stats_for(state))
        stats["sql_attempts"] = state.get("sql_attempt", 0)
        stats["report_attempts"] = state.get("report_attempt", 0)
        stats["max_repairs"] = self.deps.settings.report_max_repairs
        return stats

    def finish(self, state: RunState) -> dict[str, Any]:
        terminal = state.get("terminal") or {
            "kind": "abstain",
            "code": AbstentionCode.VERIFICATION_FAILED,
            "private": "no_terminal",
        }
        kind = terminal["kind"]
        if kind in ("published", "noop"):
            return {}
        code = terminal.get("code")
        if kind == "abstain" and not isinstance(code, AbstentionCode):
            code = AbstentionCode.VERIFICATION_FAILED
        if kind != "abstain" and not isinstance(code, ErrorCode):
            code = ErrorCode.INTERNAL
        with self._tx(state) as conn:
            run = store.lock_run(conn, state["run_id"])
            if run is None or run.status in TERMINAL_STATUSES:
                return {}
            rid, tid = state["run_id"], state["tenant_id"]
            if kind == "abstain":
                status, outcome = RunStatus.ABSTAINED, Outcome.ABSTAINED
                assert isinstance(code, AbstentionCode)  # noqa: S101 - narrowed above
                abstention = {"code": code.value, "message": ABSTENTION_MESSAGES[code]}
            else:
                status = RunStatus.CANCELLED if kind == "cancel" else RunStatus.FAILED
                outcome = Outcome.CANCELLED if kind == "cancel" else Outcome.FAILED
                abstention = None
                assert isinstance(code, ErrorCode)  # noqa: S101 - narrowed above
                store.append_event(
                    conn,
                    rid,
                    tid,
                    EventType.ERROR,
                    {
                        "code": code.value,
                        "message": ERROR_MESSAGES[code],
                    },
                )
            store.append_event(
                conn,
                rid,
                tid,
                EventType.DONE,
                {
                    "message_id": None,
                    "outcome": outcome.value,
                    "verification": None,
                    "abstention": abstention,
                    "inference": state.get("inference_mode"),
                },
            )
            stats = self._final_stats(state)
            stats["private_reason"] = terminal.get("private")
            store.update_run(
                conn,
                rid,
                status=status,
                outcome_code=str(code.value),
                stats=stats,
                finished_at="now",
            )
            store.finish_job(conn, rid)
            self._audit(
                conn,
                state,
                event_type="report.finish",
                action="dataset.query",
                resource_id=run.resource_id,
                decision="deny"
                if code in (AbstentionCode.NO_ACCESSIBLE_DATA, ErrorCode.ACCESS_REVOKED)
                else "info",
                reason=str(terminal.get("private") or code.value),
                details={"run_id": str(rid), "outcome": outcome.value, "code": code.value},
            )
        return {}

    # -- entry point ---------------------------------------------------------------

    def run(self, run_id: UUID, tenant_id: UUID, user_id: UUID, question: str, mode: str) -> None:
        state: RunState = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "question": question,
            "inference_mode": mode,
            "terminal": None,
        }
        token = self._token(state)
        stop = threading.Event()
        watcher = threading.Thread(
            target=self._watch_cancel, args=(state, token, stop), daemon=True
        )
        watcher.start()
        try:
            self.graph.invoke(state, config={"recursion_limit": 60})
        finally:
            stop.set()
            watcher.join(timeout=2)
            self._cancel_tokens.pop(run_id, None)
            self._stats.pop(run_id, None)

    def _watch_cancel(self, state: RunState, token: CancelToken, stop: threading.Event) -> None:
        while not stop.wait(0.5):
            try:
                with self._tx(state) as conn:
                    if store.cancel_requested(conn, state["run_id"]):
                        token.cancel()
                        return
            except Exception:  # noqa: S112 - watcher must never crash the run
                continue


Kind = Literal["abstain", "fail", "cancel", "published", "noop"]
