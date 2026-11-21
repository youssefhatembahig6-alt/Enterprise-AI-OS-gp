"""End-to-end report runs on real PostgreSQL with deterministic model doubles.

Every test reads the raw SSE text the browser would receive. Model doubles are the
explicit FakeModelClient; runs are labelled ``fake`` and this is integration evidence,
not model-quality evidence.
"""

from __future__ import annotations

import json
import tomllib
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.reporting.model_client import (
    CheckResult,
    FakeModelClient,
    GatewayUnavailable,
    ScriptStep,
)
from app.reporting.workflow import RunState
from tests.integration.conftest import DbHarness, owner_exec, report_settings, tenant_packs
from tests.integration.report_support import (
    NILETECH_Q1,
    Stream,
    ask,
    assert_no_figures,
    assert_only_status_before_publication,
    auth,
    replay,
    token_for,
    wait_for_run,
)

CASES = tomllib.loads(
    (Path(__file__).resolve().parents[2] / "fixtures/evaluation/sales-q1-2026.toml").read_text()
)
GENERALIZATION = tomllib.loads(
    (
        Path(__file__).resolve().parents[2] / "fixtures/evaluation/sales-generalization-m2.toml"
    ).read_text()
)
Q1_QUESTION = CASES["cases"][0]["question"]
FINANCE = "niletech/finance-reader"

GOOD_SQL = json.dumps(
    {
        "sql": "SELECT count(*) AS order_count, sum(o.subtotal) AS net_sales,"
        " sum(o.tax) AS tax_amount, sum(o.total) AS gross_sales FROM reporting.sales_orders o"
        " WHERE o.order_date >= DATE '2026-01-01' AND o.order_date <= DATE '2026-03-31'"
        " AND o.status = 'FULFILLED'"
    }
)


def sql(where: str, select: str | None = None, join: str = "") -> str:
    head = select or (
        "SELECT count(*) AS order_count, sum(o.subtotal) AS net_sales,"
        " sum(o.tax) AS tax_amount, sum(o.total) AS gross_sales"
    )
    return json.dumps({"sql": f"{head} FROM reporting.sales_orders o {join} WHERE {where}"})


Q1 = "o.order_date BETWEEN '2026-01-01' AND '2026-03-31'"
BAD_SQL = {
    "wrong_quarter": sql(
        "o.order_date BETWEEN '2026-04-01' AND '2026-06-30' AND o.status = 'FULFILLED'"
    ),
    "invoice_date": sql(
        "i.issue_date BETWEEN '2026-01-01' AND '2026-03-31' AND o.status = 'FULFILLED'",
        join="JOIN reporting.sales_invoices i ON i.order_id = o.id",
    ),
    "paid_only": sql(
        f"{Q1} AND i.status = 'PAID'", join="JOIN reporting.sales_invoices i ON i.order_id = o.id"
    ),
    "all_status": sql(Q1),
    "gross_as_net": sql(
        f"{Q1} AND o.status = 'FULFILLED'",
        select="SELECT count(*) AS order_count, sum(o.total) AS net_sales,"
        " sum(o.tax) AS tax_amount, sum(o.total) AS gross_sales",
    ),
    "line_join": sql(
        f"{Q1} AND o.status = 'FULFILLED'",
        join="JOIN reporting.sales_order_lines l ON l.order_id = o.id",
    ),
    "worked_subset": sql(
        f"{Q1} AND o.status = 'FULFILLED' AND o.order_number IN"
        " ('SO-001815','SO-001809','SO-001082','SO-001890','SO-001990')"
    ),
    "malformed": "Sure! Here is the SQL you asked for: SELECT everything",
    "multiple_statements": json.dumps({"sql": "SELECT 1; DELETE FROM reporting.sales_orders"}),
    "writable_cte": json.dumps(
        {"sql": "WITH d AS (DELETE FROM reporting.sales_orders RETURNING 1) SELECT count(*) FROM d"}
    ),
    "set_tenant": json.dumps({"sql": "SET app.tenant_id = '00000000-0000-0000-0000-000000000000'"}),
}
GOOD_REPORT = json.dumps(
    {
        "sentences": [
            {
                "text": "In Q1 2026, net sales (tax-exclusive) were USD 80,934,385.00 from 291 "
                "fulfilled orders.",
                "cites": ["net_sales", "order_count"],
            },
            {
                "text": "Tax was USD 11,330,813.90, so gross sales were USD 92,265,198.90.",
                "cites": ["tax_amount", "gross_sales"],
            },
        ]
    }
)
SUBSET_DRAFT = json.dumps(
    {"sentences": [{"text": "Q1 2026 net sales totalled USD 593,489.00.", "cites": ["net_sales"]}]}
)
GROSS_AS_NET_DRAFT = json.dumps(
    {"sentences": [{"text": "Net sales were USD 92,265,198.90.", "cites": ["gross_sales"]}]}
)
REJECTED_MARKER = "REJECTED-DRAFT-MARKER"
# Numerically valid (passes deterministic checks); only the claim checker rejects it.
UNSUPPORTED_DRAFT = json.dumps(
    {
        "sentences": [
            {
                "text": f"Net sales of USD 80,934,385.00 were driven by {REJECTED_MARKER}.",
                "cites": ["net_sales"],
            }
        ]
    }
)


def marker_checker(premise: str, hypothesis: str) -> CheckResult:
    score = 0.02 if REJECTED_MARKER in hypothesis else 0.97
    return CheckResult(entailment=score, neutral=0.0, contradiction=1 - score)


@pytest.fixture
def make_client(report_db: DbHarness, stub_http_url: str) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def build(model: Any = None, **overrides: Any) -> TestClient:
        app = create_app(report_settings(report_db, stub_http_url, **overrides), model=model)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


def hook(client: TestClient, fn: Callable[[str, RunState], None]) -> None:
    client.app.state.report_runner.workflow.deps.before_node = fn  # type: ignore[attr-defined]


def assert_published_q1(stream: Stream) -> dict[str, Any]:
    assert stream.status_code == 200
    assert stream.done["outcome"] == "published", stream.raw[-600:]
    assert_only_status_before_publication(stream)
    (table_frame,) = [f for f in stream.of("token") if f.data["part"] == "table"]
    cells = {c["column"]: c for c in table_frame.data["table"]["rows"][0]}
    assert {k: v["value"] for k, v in cells.items()} == NILETECH_Q1
    evidence_ids = {c["ref"]["evidence_id"] for c in cells.values()}
    assert len(evidence_ids) == 1
    for frame in stream.of("citation"):
        assert frame.data["evidence_id"] in evidence_ids
        assert frame.data["row"] == 0
    ids = [f.id for f in stream.frames if f.id is not None]
    assert len(ids) == len(stream.frames)
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    return stream.done


# -- correct reports -------------------------------------------------------------------


def test_frozen_q1_report_is_published_with_evidence(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    client = make_client(FakeModelClient({"sql": [GOOD_SQL], "report": [GOOD_REPORT]}))
    token = token_for(client, report_db, FINANCE)
    stream = ask(client, token, Q1_QUESTION)
    done = assert_published_q1(stream)
    verification = done["verification"]
    assert verification["label"] == "figures_verified_narrative_dev_checked"
    assert verification["inference_mode"] == "fake"
    assert "FAKE INFERENCE" in verification["inference_label"]
    for frame in stream.of("status"):
        assert (
            frame.data["inference"] == "fake" and "FAKE INFERENCE" in frame.data["inference_label"]
        )
    texts = [f.data["text"] for f in stream.of("token") if f.data["part"] == "text"]
    assert texts and "80,934,385.00" in texts[0]

    run = client.get(f"/api/v1/runs/{stream.run_id}", headers=auth(token)).json()
    assert run["status"] == "published" and run["inference_mode"] == "fake"
    evidence_id = run["result"]["evidence"]["evidence_id"]
    evidence = client.get(f"/api/v1/evidence/{evidence_id}", headers=auth(token))
    assert evidence.status_code == 200
    body = evidence.json()
    assert body["integrity"] == "verified"
    assert [c["name"] for c in body["columns"]] == list(NILETECH_Q1)
    assert body["rows"] == [list(NILETECH_Q1.values())]
    assert body["parameters"] == {"row_limit": 201}
    assert body["dataset_fingerprint"].endswith(CASES["normalized_fingerprint"])
    assert body["semantics"]["verified"]["window"] == ["2026-01-01", "2026-03-31"]
    assert body["semantics"]["verified"]["statuses"] == ["FULFILLED"]
    assert body["semantics"]["join_probe"]["joined_rows"] == 291
    assert "o.order_date" in body["executed_sql"]


def test_default_fake_model_publishes_q1(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    client = make_client()  # MODEL_MODE=fake builds the default deterministic double
    stream = ask(client, token_for(client, report_db, FINANCE), Q1_QUESTION)
    assert_published_q1(stream)


@pytest.mark.parametrize("case", GENERALIZATION["cases"], ids=lambda c: c["id"])
def test_other_periods_use_runtime_verification_not_stored_answers(
    make_client: Callable[..., TestClient], report_db: DbHarness, case: dict[str, Any]
) -> None:
    client = make_client()
    stream = ask(client, token_for(client, report_db, FINANCE), case["question"])
    assert stream.done["outcome"] == "published", stream.raw[-500:]
    (table,) = [f for f in stream.of("token") if f.data["part"] == "table"]
    values = {c["column"]: c["value"] for c in table.data["table"]["rows"][0]}
    assert values == {
        "order_count": case["expected_order_count"],
        "net_sales": case["expected_net"],
        "tax_amount": case["expected_tax"],
        "gross_sales": case["expected_gross"],
    }
    assert case["distractor_all_status_net"] not in stream.raw


# -- semantic failures, repairs and abstention -------------------------------------------


@pytest.mark.parametrize("bad", sorted(BAD_SQL))
def test_bad_sql_is_repaired_and_never_shown(
    make_client: Callable[..., TestClient], report_db: DbHarness, bad: str
) -> None:
    fake = FakeModelClient({"sql": [BAD_SQL[bad], GOOD_SQL], "report": [GOOD_REPORT]})
    client = make_client(fake)
    stream = ask(client, token_for(client, report_db, FINANCE), Q1_QUESTION)
    assert_published_q1(stream)
    retry = [f for f in stream.of("status") if f.data["state"] == "retrying"]
    assert retry and retry[0].data["attempt"] == 2 and retry[0].data["max_attempts"] == 3
    # Distractor totals from the rejected query never reach the browser.
    for distractor in ("102987786", "89841424", "593489", "593,489"):
        assert distractor not in stream.raw
    # The repair prompt carried rejection codes, not rows.
    repair_prompt = fake.calls[1][1][-1]["content"]
    assert "rejected" in repair_prompt and "80934385" not in repair_prompt
    stats = owner_exec(
        report_db, "SELECT stats FROM report_runs WHERE id = :id", {"id": stream.run_id}
    ).scalar_one()
    assert stats["sql_attempts"] == 2 and stats["rejections"]


def test_exhausted_sql_repairs_abstain_without_figures(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    fake = FakeModelClient(
        {"sql": [BAD_SQL["all_status"], BAD_SQL["line_join"], BAD_SQL["gross_as_net"], GOOD_SQL]}
    )
    client = make_client(fake)
    stream = ask(client, token_for(client, report_db, FINANCE), Q1_QUESTION)
    assert stream.done["outcome"] == "abstained"
    assert stream.done["abstention"]["code"] == "verification_failed"
    assert [p for p, _ in fake.calls].count("sql") == 3  # first draft + two repairs
    assert set(stream.events()) <= {"status", "done"}
    assert_no_figures(stream.raw)
    evidence = owner_exec(
        report_db, "SELECT count(*) FROM report_evidence WHERE run_id = :id", {"id": stream.run_id}
    ).scalar_one()
    assert evidence == 0


@pytest.mark.parametrize(
    "drafts",
    [
        [SUBSET_DRAFT, GOOD_REPORT],
        [GROSS_AS_NET_DRAFT, GOOD_REPORT],
        ["not json at all", GOOD_REPORT],
    ],
    ids=["fabricated_subset_total", "gross_described_as_net", "malformed"],
)
def test_bad_narrative_is_repaired_and_hidden(
    make_client: Callable[..., TestClient], report_db: DbHarness, drafts: list[str]
) -> None:
    script: dict[str, list[ScriptStep]] = {"sql": [GOOD_SQL], "report": list(drafts)}
    client = make_client(FakeModelClient(script))
    stream = ask(client, token_for(client, report_db, FINANCE), Q1_QUESTION)
    assert_published_q1(stream)
    assert "593,489.00" not in stream.raw and "593489" not in stream.raw
    texts = [f.data["text"] for f in stream.of("token") if f.data["part"] == "text"]
    assert texts == [s["text"] for s in json.loads(GOOD_REPORT)["sentences"]]


def test_exhausted_narrative_repairs_publish_structured_only(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    fake = FakeModelClient(
        {"sql": [GOOD_SQL], "report": [SUBSET_DRAFT, UNSUPPORTED_DRAFT, GROSS_AS_NET_DRAFT]},
        checker=marker_checker,
    )
    client = make_client(fake)
    stream = ask(client, token_for(client, report_db, FINANCE), Q1_QUESTION)
    done = assert_published_q1(stream)
    assert done["verification"]["label"] == "figures_verified_structured_only"
    assert done["verification"]["narrative"] == "withheld_failed_verification"
    assert [f for f in stream.of("token") if f.data["part"] == "text"] == []
    assert REJECTED_MARKER not in stream.raw and "593,489" not in stream.raw
    assert [p for p, _ in fake.calls].count("report") == 3


def test_checker_rejection_hides_unsupported_narrative(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    fake = FakeModelClient(
        {"sql": [GOOD_SQL], "report": [UNSUPPORTED_DRAFT, GOOD_REPORT]}, checker=marker_checker
    )
    client = make_client(fake)
    stream = ask(client, token_for(client, report_db, FINANCE), Q1_QUESTION)
    done = assert_published_q1(stream)
    assert done["verification"]["narrative"] == "verified"
    assert REJECTED_MARKER not in stream.raw
    stats = owner_exec(
        report_db, "SELECT stats FROM report_runs WHERE id = :id", {"id": stream.run_id}
    ).scalar_one()
    assert "report:claim_not_supported" in stats["rejections"]
    assert REJECTED_MARKER not in json.dumps(stats)


def test_checker_failure_publishes_only_deterministic_results(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    fake = FakeModelClient(
        {"sql": [GOOD_SQL], "report": [GOOD_REPORT]}, checker=GatewayUnavailable("checker down")
    )
    client = make_client(fake)
    stream = ask(client, token_for(client, report_db, FINANCE), Q1_QUESTION)
    done = assert_published_q1(stream)
    assert done["verification"]["label"] == "figures_verified_structured_only"
    assert done["verification"]["narrative"] == "withheld_checker_unavailable"
    assert done["verification"]["checker"] is None
    # The (unchecked) draft sentence is not shown anywhere.
    assert "so gross sales were" not in stream.raw


@pytest.mark.parametrize("stage", ["sql", "router"])
def test_unavailable_inference_fails_without_partial_output(
    make_client: Callable[..., TestClient], report_db: DbHarness, stage: str
) -> None:
    fake = FakeModelClient({stage: [GatewayUnavailable("connect refused")]})
    client = make_client(fake)
    question = Q1_QUESTION if stage == "sql" else "Give me the numbers for Q1 2026"
    stream = ask(client, token_for(client, report_db, FINANCE), question)
    assert stream.of("error")[0].data["code"] == "model_unavailable"
    assert stream.done["outcome"] == "failed"
    assert set(stream.events()) <= {"status", "error", "done"}
    assert_no_figures(stream.raw)


def test_unconfigured_gateway_reports_model_unavailable(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    client = make_client(None, MODEL_MODE="gateway", LLM_BASE_URL="")
    stream = ask(client, token_for(client, report_db, FINANCE), Q1_QUESTION)
    assert stream.of("error")[0].data["code"] == "model_unavailable"
    assert stream.done["inference"] == "gateway"
    assert_no_figures(stream.raw)


@pytest.mark.parametrize(
    ("question", "code"),
    [
        ("What were our sales?", "clarification_needed"),
        ("Sales for Q1 2026 including pending orders", "unsupported_request"),
        ("Collected (paid) sales for Q1 2026", "unsupported_request"),
        ("Q1 2026 sales by region", "unsupported_request"),
        ("Fulfilled sales for Q3 2026", "period_unavailable"),
    ],
)
def test_unsupported_or_ambiguous_requests_abstain(
    make_client: Callable[..., TestClient], report_db: DbHarness, question: str, code: str
) -> None:
    fake = FakeModelClient()
    client = make_client(fake)
    stream = ask(client, token_for(client, report_db, FINANCE), question)
    assert stream.done["outcome"] == "abstained"
    assert stream.done["abstention"]["code"] == code
    assert not [p for p, _ in fake.calls if p == "sql"]
    assert_no_figures(stream.raw)


# -- authorization ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "niletech/sales-reader",
        "niletech/denied-reader",
        "niletech/board-member",
        "isolation-test/finance-reader",
    ],
)
def test_denied_and_isolated_users_get_the_same_abstention(
    make_client: Callable[..., TestClient], report_db: DbHarness, ref: str
) -> None:
    fake = FakeModelClient()
    client = make_client(fake)
    # M4: the second tenant has no sales_reports pack; enable it here so this test still
    # proves the cross-tenant dataset boundary rather than the tenant configuration.
    with tenant_packs(report_db, ref.split("/")[0], ["documents", "sales_reports"]):
        stream = ask(client, token_for(client, report_db, ref), Q1_QUESTION)
    assert stream.done["outcome"] == "abstained"
    assert stream.done["abstention"] == {
        "code": "no_accessible_data",
        "message": "No data you are authorized to access answers this question.",
    }
    assert fake.calls == []  # no model call, no SQL
    assert set(stream.events()) <= {"status", "done"}
    assert_no_figures(stream.raw)
    for value in CASES["cases"][1]["forbidden_values"]:
        assert value not in stream.raw


def test_isolation_boundary_case_abstains_even_when_authorized(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    case = CASES["cases"][1]
    client = make_client(FakeModelClient())
    slug = case["asking_identity"].split("/")[0]
    with tenant_packs(report_db, slug, ["documents", "sales_reports"]):  # M4: see above
        token = token_for(client, report_db, case["asking_identity"])
        stream = ask(client, token, case["question"])
    assert stream.done["outcome"] == case["expected_kind"].replace("abstention", "abstained")
    stats = owner_exec(
        report_db, "SELECT stats FROM report_runs WHERE id = :id", {"id": stream.run_id}
    ).scalar_one()
    assert stats["private_reason"] == "no_dataset"


def test_missing_and_deactivated_users(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    client = make_client(FakeModelClient())
    assert client.post("/api/v1/chat", json={"question": Q1_QUESTION}).status_code == 401
    bogus = client.post("/api/v1/chat", json={"question": Q1_QUESTION}, headers=auth("not-a-token"))
    assert bogus.status_code == 401
    token = token_for(client, report_db, FINANCE)
    user = report_db.foundation.tenants["niletech"].users["finance-reader"].id
    owner_exec(report_db, "UPDATE users SET status = 'inactive' WHERE id = :u", {"u": user})
    try:
        response = client.post("/api/v1/chat", json={"question": Q1_QUESTION}, headers=auth(token))
        assert response.status_code == 401
    finally:
        owner_exec(report_db, "UPDATE users SET status = 'active' WHERE id = :u", {"u": user})


@pytest.fixture
def revoke_finance_team(report_db: DbHarness) -> Iterator[Callable[[], None]]:
    tenant = report_db.foundation.tenants["niletech"]
    group, user = tenant.groups["finance-team"].id, tenant.users["finance-reader"].id

    def revoke() -> None:
        owner_exec(
            report_db,
            "DELETE FROM group_memberships WHERE group_id = :g AND user_id = :u",
            {"g": group, "u": user},
        )

    yield revoke
    owner_exec(
        report_db,
        "INSERT INTO group_memberships (tenant_id, group_id, user_id) VALUES (:t, :g, :u)"
        " ON CONFLICT DO NOTHING",
        {"t": tenant.id, "g": group, "u": user},
    )


@pytest.mark.parametrize("stage", ["execute", "persist_evidence", "draft_report", "publish"])
def test_revocation_during_a_run_prevents_publication(
    make_client: Callable[..., TestClient],
    report_db: DbHarness,
    revoke_finance_team: Callable[[], None],
    stage: str,
) -> None:
    client = make_client(FakeModelClient({"sql": [GOOD_SQL], "report": [GOOD_REPORT]}))
    token = token_for(client, report_db, FINANCE)

    def before(node: str, state: RunState) -> None:
        if node == stage:
            revoke_finance_team()

    hook(client, before)
    stream = ask(client, token, Q1_QUESTION)
    assert stream.done["outcome"] == "failed"
    error_codes = {f.data["code"] for f in stream.of("error")}
    assert error_codes == {"access_revoked"}
    assert not stream.of("token") and not stream.of("citation")
    assert_no_figures(stream.raw)
    run = owner_exec(
        report_db, "SELECT status, result FROM report_runs WHERE id = :id", {"id": stream.run_id}
    ).one()
    assert run.status == "failed" and run.result is None


def test_policy_deny_grant_added_before_publication(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    tenant = report_db.foundation.tenants["niletech"]
    resource, user = tenant.resources["sales-reporting"].id, tenant.users["finance-reader"].id
    client = make_client(FakeModelClient({"sql": [GOOD_SQL], "report": [GOOD_REPORT]}))
    token = token_for(client, report_db, FINANCE)

    def before(node: str, state: RunState) -> None:
        if node == "publish":
            owner_exec(
                report_db,
                "INSERT INTO resource_grants (tenant_id, resource_id, user_id, effect, action)"
                " VALUES (:t, :r, :u, 'deny', 'dataset.query')",
                {"t": tenant.id, "r": resource, "u": user},
            )

    hook(client, before)
    try:
        stream = ask(client, token, Q1_QUESTION)
        assert stream.done["outcome"] == "failed"
        assert_no_figures(stream.raw)
    finally:
        owner_exec(
            report_db,
            "DELETE FROM resource_grants WHERE resource_id = :r AND user_id = :u AND effect = 'deny'",
            {"r": resource, "u": user},
        )


# -- replay, revocation after publication, foreign ids ------------------------------------


def test_reconnect_replays_identical_events(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    client = make_client(FakeModelClient({"sql": [GOOD_SQL], "report": [GOOD_REPORT]}))
    token = token_for(client, report_db, FINANCE)
    live = ask(client, token, Q1_QUESTION)
    assert live.run_id is not None
    full = replay(client, token, live.run_id)
    assert [(f.id, f.event, f.data) for f in full.frames] == [
        (f.id, f.event, f.data) for f in live.frames
    ]
    middle = live.frames[3].id
    assert middle is not None
    tail = replay(client, token, live.run_id, after=middle)
    assert [f.id for f in tail.frames] == [f.id for f in live.frames if f.id and f.id > middle]
    by_header = replay(client, token, live.run_id, headers={"Last-Event-ID": str(middle)})
    assert [f.id for f in by_header.frames] == [f.id for f in tail.frames]


def test_replay_and_evidence_after_revocation_expose_nothing(
    make_client: Callable[..., TestClient],
    report_db: DbHarness,
    revoke_finance_team: Callable[[], None],
) -> None:
    client = make_client(FakeModelClient({"sql": [GOOD_SQL], "report": [GOOD_REPORT]}))
    token = token_for(client, report_db, FINANCE)
    live = ask(client, token, Q1_QUESTION)
    assert_published_q1(live)
    evidence_id = live.of("citation")[0].data["evidence_id"]
    revoke_finance_team()
    stream = replay(client, token, str(live.run_id))
    assert stream.status_code == 200
    assert not stream.of("token") and not stream.of("citation")
    assert stream.of("error")[-1].data["code"] == "access_revoked"
    assert_no_figures(stream.raw)
    assert client.get(f"/api/v1/evidence/{evidence_id}", headers=auth(token)).status_code == 404
    run = client.get(f"/api/v1/runs/{live.run_id}", headers=auth(token)).json()
    assert run["result"] is None and run["outcome_code"] == "access_revoked"
    assert_no_figures(json.dumps(run))


def test_foreign_and_unknown_ids_are_not_found(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    client = make_client(FakeModelClient({"sql": [GOOD_SQL], "report": [GOOD_REPORT]}))
    owner = token_for(client, report_db, FINANCE)
    live = ask(client, owner, Q1_QUESTION)
    evidence_id = live.of("citation")[0].data["evidence_id"]
    for ref in ("niletech/sales-reader", "niletech/denied-reader", "isolation-test/finance-reader"):
        other = token_for(client, report_db, ref)
        assert replay(client, other, str(live.run_id)).status_code == 404
        assert client.get(f"/api/v1/runs/{live.run_id}", headers=auth(other)).status_code == 404
        assert (
            client.post(f"/api/v1/runs/{live.run_id}/cancel", headers=auth(other)).status_code
            == 404
        )
        response = client.get(f"/api/v1/evidence/{evidence_id}", headers=auth(other))
        assert response.status_code == 404
        assert_no_figures(response.text)
    unknown = "00000000-0000-4000-8000-000000000000"
    assert client.get(f"/api/v1/evidence/{unknown}", headers=auth(owner)).status_code == 404
    assert replay(client, owner, unknown).status_code == 404


# -- cancellation, idempotency, durable jobs ------------------------------------------------


def test_cancellation_stops_the_run_before_publication(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    fake = FakeModelClient({"sql": [GOOD_SQL], "report": [GOOD_REPORT]})
    client = make_client(fake)
    token = token_for(client, report_db, FINANCE)

    def before(node: str, state: RunState) -> None:
        if node == "draft_report":
            response = client.post(f"/api/v1/runs/{state['run_id']}/cancel", headers=auth(token))
            assert response.status_code == 202

    hook(client, before)
    stream = ask(client, token, Q1_QUESTION)
    assert stream.done["outcome"] == "cancelled"
    assert stream.of("error")[0].data["code"] == "cancelled"
    assert not stream.of("token")
    assert_no_figures(stream.raw)
    assert "report" not in [p for p, _ in fake.calls]


def test_idempotent_create_and_duplicate_job_delivery(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    client = make_client(FakeModelClient({"sql": [GOOD_SQL], "report": [GOOD_REPORT]}))
    token = token_for(client, report_db, FINANCE)
    first = ask(client, token, Q1_QUESTION, client_request_id="retry-key-0001")
    second = ask(client, token, Q1_QUESTION, client_request_id="retry-key-0001")
    assert first.run_id == second.run_id
    assert [f.id for f in first.frames] == [f.id for f in second.frames]
    runner = client.app.state.report_runner  # type: ignore[attr-defined]
    tenant = report_db.foundation.tenants["niletech"].id
    assert first.run_id is not None
    assert runner.run_job(uuid.UUID(first.run_id), tenant) == "already_finished"
    counts = owner_exec(
        report_db,
        "SELECT count(*) FILTER (WHERE event_type = 'done') AS done,"
        " count(*) FILTER (WHERE event_type = 'token') AS tokens FROM report_run_events"
        " WHERE run_id = :id",
        {"id": first.run_id},
    ).one()
    assert counts.done == 1
    runs = owner_exec(
        report_db, "SELECT count(*) FROM report_runs WHERE client_request_id = 'retry-key-0001'"
    ).scalar_one()
    assert runs == 1


def test_outbox_redispatches_a_lost_job(
    make_client: Callable[..., TestClient], report_db: DbHarness
) -> None:
    """A run whose queue message was lost is picked up from the job row."""

    from app.reporting.service import dispatch_due_jobs

    client = make_client(FakeModelClient({"sql": [GOOD_SQL], "report": [GOOD_REPORT]}))
    token = token_for(client, report_db, FINANCE)
    service = client.app.state.reports  # type: ignore[attr-defined]
    real = service._dispatcher
    service._dispatcher = type("Lost", (), {"dispatch": lambda *_: None})()
    ctx = client.app.state.identity.authenticate(token, "t")  # type: ignore[attr-defined]
    run = service.create_run(ctx, Q1_QUESTION, "lost-message-0001")
    service._dispatcher = real
    job = owner_exec(
        report_db, "SELECT status FROM report_jobs WHERE run_id = :id", {"id": run.id}
    ).scalar_one()
    assert job == "pending"
    assert dispatch_due_jobs(client.app.state.engine, real) >= 1  # type: ignore[attr-defined]
    assert wait_for_run(report_db, str(run.id)) == "published"
    assert (
        owner_exec(
            report_db, "SELECT status FROM report_jobs WHERE run_id = :id", {"id": run.id}
        ).scalar_one()
        == "done"
    )


def test_audit_records_report_decisions_without_figures(report_db: DbHarness) -> None:
    rows = owner_exec(
        report_db,
        "SELECT event_type, decision, reason, details::text AS details FROM audit_events"
        " WHERE event_type LIKE 'report.%'",
    ).all()
    kinds = {r.event_type for r in rows}
    assert {
        "report.run",
        "report.authorize",
        "report.query",
        "report.publish",
        "report.finish",
        "report.evidence",
    } <= kinds
    assert any(r.decision == "deny" for r in rows if r.event_type == "report.authorize")
    for row in rows:
        assert_no_figures(row.details)
        assert "SELECT" not in row.details


# -- review regressions -----------------------------------------------------------------


def lossy(sql_json: str, pattern: str) -> str:
    sql_text = json.loads(sql_json)["sql"]
    for col in ("subtotal", "tax", "total"):
        sql_text = sql_text.replace(f"sum(o.{col})", pattern.format(col=col))
    return json.dumps({"sql": sql_text})


ROUNDED_FIGURES = ("80934385,", '"80934385"', "11330814", "11,330,814", "92265199", "92,265,199")


@pytest.mark.parametrize(
    "pattern",
    ["sum(o.{col})::numeric(20,0)", "sum(o.{col}::integer)", "round(sum(o.{col}), 0)"],
    ids=["cast_around_aggregate", "cast_inside_aggregate", "round_to_units"],
)
def test_rounded_amounts_never_receive_a_verified_label(
    make_client: Callable[..., TestClient], report_db: DbHarness, pattern: str
) -> None:
    bad = lossy(GOOD_SQL, pattern)
    client = make_client(FakeModelClient({"sql": [bad, bad, bad], "report": [GOOD_REPORT]}))
    stream = ask(client, token_for(client, report_db, FINANCE), Q1_QUESTION)
    assert stream.done["outcome"] == "abstained"
    assert stream.done["verification"] is None
    assert not stream.of("token") and not stream.of("citation")
    for figure in ROUNDED_FIGURES:
        assert figure not in stream.raw
    assert (
        owner_exec(
            report_db,
            "SELECT count(*) FROM report_evidence WHERE run_id = :id",
            {"id": stream.run_id},
        ).scalar_one()
        == 0
    )


def test_runtime_scale_check_blocks_rounding_the_ast_missed(
    make_client: Callable[..., TestClient], report_db: DbHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: even if the AST cast rule were bypassed, a sum without exactly
    two decimals is never published as exact."""

    from app.reporting import semantics

    monkeypatch.setattr(semantics, "cast_preserves_value", lambda node, kind: True)
    bad = lossy(GOOD_SQL, "sum(o.{col})::numeric(20,0)")
    client = make_client(FakeModelClient({"sql": [bad, GOOD_SQL], "report": [GOOD_REPORT]}))
    stream = ask(client, token_for(client, report_db, FINANCE), Q1_QUESTION)
    assert_published_q1(stream)  # repaired: the exact query is what got published
    for figure in ROUNDED_FIGURES:
        assert figure not in stream.raw
    stats = owner_exec(
        report_db, "SELECT stats FROM report_runs WHERE id = :id", {"id": stream.run_id}
    ).scalar_one()
    assert "sql:value_changing_cast" in stats["rejections"]


def _replay_until(
    client: TestClient, token: str, run_id: str, stop: Callable[[str], bool]
) -> tuple[Iterator[str], list[str]]:
    service = client.app.state.reports  # type: ignore[attr-defined]
    rid = uuid.UUID(run_id)
    frames = service.stream(token, service.open_stream(token, rid), rid, 0)
    seen: list[str] = []
    for frame in frames:
        seen.append(frame)
        if stop(frame):
            break
    return frames, seen


@pytest.mark.parametrize(
    "stop_after",
    ["event: status\n", "event: token\n", "event: citation\n"],
    ids=["after_status", "after_first_token", "after_first_citation"],
)
def test_revocation_between_frames_of_one_batch_stops_content(
    make_client: Callable[..., TestClient],
    report_db: DbHarness,
    revoke_finance_team: Callable[[], None],
    stop_after: str,
) -> None:
    """All events of a finished run arrive in one fetched batch; revoking access
    between two yields must stop every later content frame and the published done."""

    client = make_client(FakeModelClient({"sql": [GOOD_SQL], "report": [GOOD_REPORT]}))
    token = token_for(client, report_db, FINANCE)
    finished = ask(client, token, Q1_QUESTION)
    assert finished.done["outcome"] == "published" and finished.run_id
    frames, before = _replay_until(client, token, finished.run_id, lambda f: stop_after in f)
    revoke_finance_team()
    after = list(frames)
    assert not [f for f in after if "event: token\n" in f or "event: citation\n" in f]
    assert not [f for f in after if "event: done\n" in f]
    # Non-content status frames may still flow; the stream then ends with the refusal,
    # which is not a persisted event.
    assert '"access_revoked"' in after[-1] and "id:" not in after[-1]
    assert all("event: status\n" in f for f in after[:-1])
    assert before  # some frames were legitimately sent before the revocation
