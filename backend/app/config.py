"""Application settings loaded from environment variables.

Secrets are ``SecretStr`` so they never appear in ``repr`` output or logs.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_PLACEHOLDER_MARKERS = ("change-me", "changeme", "example")


class ModelMode(StrEnum):
    # Real inference through the external gateway (Colab GPU). Never on the laptop.
    GATEWAY = "gateway"
    # Explicit deterministic double for development and tests. Every run, event and
    # screen produced in this mode is labelled as fake inference.
    FAKE = "fake"


class ReportDispatch(StrEnum):
    CELERY = "celery"  # Compose: durable job row + Celery worker
    INLINE = "inline"  # tests and single-process development: API thread pool


class DocumentStoreKind(StrEnum):
    S3 = "s3"
    FILESYSTEM = "filesystem"


class RetrievalMode(StrEnum):
    DENSE = "dense"
    HYBRID = "hybrid"  # dense + sparse prefetches fused with RRF


class RelevanceFilter(StrEnum):
    """Which retrieved passages reach the answer model (ADR 0304, M3 relevance study)."""

    LEXICAL = "lexical"  # keep passages sharing a significant term with the question
    DENSE_SCORE = "dense_score"  # keep passages whose dense cosine >= RELEVANCE_MIN_SCORE
    NONE = "none"  # keep every authorized hit; the answer model and verifier decide


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    environment: Environment = Field(default=Environment.DEVELOPMENT, alias="EAIOS_ENV")

    # Application database role (non-owner, NOBYPASSRLS). Never the migration role.
    database_url: SecretStr = Field(alias="DATABASE_URL")
    database_pool_size: int = Field(default=5, alias="DATABASE_POOL_SIZE", ge=1, le=50)

    redis_url: SecretStr = Field(alias="REDIS_URL")
    qdrant_url: str = Field(alias="QDRANT_URL")
    s3_endpoint_url: str = Field(alias="S3_ENDPOINT_URL")

    # External model gateway (Colab). One root URL; routes are composed from the
    # explicit paths below (docs/contracts/model-gateway.md). The root must not end
    # in /v1: the chat and embedding paths already carry it.
    model_gateway_url: str | None = Field(default=None, alias="LLM_BASE_URL")
    model_mode: ModelMode = Field(default=ModelMode.GATEWAY, alias="MODEL_MODE")
    model_gateway_token: SecretStr | None = Field(default=None, alias="MODEL_GATEWAY_TOKEN")
    model_chat_path: str = Field(default="/v1/chat/completions", alias="MODEL_CHAT_PATH")
    model_embed_path: str = Field(default="/v1/embeddings", alias="MODEL_EMBED_PATH")
    model_check_path: str = Field(default="/check", alias="MODEL_CHECK_PATH")
    model_health_path: str = Field(default="/health", alias="MODEL_HEALTH_PATH")
    # M3: Docling (M1 parser choice for PDFs) runs behind the gateway.
    model_parse_path: str = Field(default="/parse", alias="MODEL_PARSE_PATH")
    docling_timeout_seconds: float = Field(
        default=600.0, alias="DOCLING_TIMEOUT_SECONDS", gt=0, le=1800
    )
    use_docling: bool = Field(default=True, alias="USE_DOCLING")
    # Measured M1 T4 configuration (ml/gpu-feasibility/reports/2026-11-20-target-t4.md).
    model_chat_name: str = Field(default="Qwen/Qwen3-8B-AWQ", alias="MODEL_CHAT_NAME")
    model_chat_revision: str = Field(
        default="4da05a8edb55c6046cce958586c33b61da07bb79", alias="MODEL_CHAT_REVISION"
    )
    model_embed_name: str = Field(default="BAAI/bge-m3", alias="MODEL_EMBED_NAME")
    model_embed_revision: str = Field(
        default="5617a9f61b028005a4858fdac845db406aefb181", alias="MODEL_EMBED_REVISION"
    )
    model_checker_name: str = Field(
        default="MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli", alias="MODEL_CHECKER_NAME"
    )
    model_checker_revision: str = Field(
        default="6f5cf0a2b59cabb106aca4c287eed12e357e90eb", alias="MODEL_CHECKER_REVISION"
    )
    model_expected_gpu_memory_utilization: float = Field(
        default=0.75, alias="MODEL_EXPECTED_GPU_MEMORY_UTILIZATION", gt=0, lt=1
    )
    model_expected_max_model_len: int = Field(
        default=4096, alias="MODEL_EXPECTED_MAX_MODEL_LEN", ge=512
    )
    model_connect_timeout_seconds: float = Field(
        default=5.0, alias="MODEL_CONNECT_TIMEOUT_SECONDS", gt=0, le=60
    )
    # Longest gap between streamed chunks before the call fails.
    model_read_timeout_seconds: float = Field(
        default=60.0, alias="MODEL_READ_TIMEOUT_SECONDS", gt=0, le=600
    )
    # Whole-call deadline (the measured 400-token report took 113.5 s at 0.75).
    model_call_deadline_seconds: float = Field(
        default=240.0, alias="MODEL_CALL_DEADLINE_SECONDS", gt=0, le=1800
    )
    # Development NLI baseline threshold: uncalibrated (M1 report); M7 calibrates.
    checker_entailment_threshold: float = Field(
        default=0.5, alias="CHECKER_ENTAILMENT_THRESHOLD", gt=0, lt=1
    )

    # Separate read-only report query role (M2). Unset: reports are unavailable.
    report_database_url: SecretStr | None = Field(default=None, alias="REPORT_DATABASE_URL")
    report_pool_size: int = Field(default=2, alias="REPORT_POOL_SIZE", ge=1, le=8)
    report_statement_timeout_ms: int = Field(
        default=5000, alias="REPORT_STATEMENT_TIMEOUT_MS", ge=100, le=30000
    )
    report_max_rows: int = Field(default=200, alias="REPORT_MAX_ROWS", ge=1, le=5000)
    report_max_repairs: int = Field(default=2, alias="REPORT_MAX_REPAIRS", ge=0, le=2)
    report_dispatch: ReportDispatch = Field(default=ReportDispatch.CELERY, alias="REPORT_DISPATCH")
    report_job_lease_seconds: int = Field(
        default=900, alias="REPORT_JOB_LEASE_SECONDS", ge=30, le=3600
    )
    report_job_max_attempts: int = Field(default=3, alias="REPORT_JOB_MAX_ATTEMPTS", ge=1, le=10)
    # M3 documents: raw bytes in S3-compatible storage (SeaweedFS) or a local directory.
    document_store: DocumentStoreKind = Field(default=DocumentStoreKind.S3, alias="DOCUMENT_STORE")
    document_fs_root: str = Field(default="/app/var/documents", alias="DOCUMENT_FS_ROOT")
    s3_bucket: str = Field(default="eaios-raw", alias="S3_BUCKET")
    s3_access_key: SecretStr = Field(default=SecretStr("eaios-dev"), alias="S3_ACCESS_KEY")
    s3_secret_key: SecretStr = Field(default=SecretStr("eaios-dev-secret"), alias="S3_SECRET_KEY")
    qdrant_collection_prefix: str = Field(
        default="eaios_chunks", alias="QDRANT_COLLECTION_PREFIX", pattern=r"^[a-z][a-z0-9_]{0,40}$"
    )
    # Start dense (roadmap); switch to hybrid only after the recorded comparison.
    retrieval_mode: RetrievalMode = Field(default=RetrievalMode.DENSE, alias="RETRIEVAL_MODE")
    retrieval_top_k: int = Field(default=5, alias="RETRIEVAL_TOP_K", ge=1, le=10)
    retrieval_prefetch: int = Field(default=20, alias="RETRIEVAL_PREFETCH", ge=5, le=100)
    relevance_filter: RelevanceFilter = Field(
        default=RelevanceFilter.LEXICAL, alias="RELEVANCE_FILTER"
    )
    relevance_min_score: float = Field(default=0.0, alias="RELEVANCE_MIN_SCORE", ge=-1.0, le=1.0)
    ingestion_job_lease_seconds: int = Field(
        default=600, alias="INGESTION_JOB_LEASE_SECONDS", ge=30, le=3600
    )
    ingestion_job_max_attempts: int = Field(
        default=3, alias="INGESTION_JOB_MAX_ATTEMPTS", ge=1, le=10
    )

    # M4 Google Drive connector: operator-owned JSON binding each tenant slug to its
    # server-side credentials and allowed root folders (app/connectors/drive.py).
    drive_connector_config: str | None = Field(default=None, alias="DRIVE_CONNECTOR_CONFIG")
    drive_sync_lease_seconds: int = Field(
        default=900, alias="DRIVE_SYNC_LEASE_SECONDS", ge=60, le=7200
    )

    sse_poll_interval_seconds: float = Field(
        default=0.5, alias="SSE_POLL_INTERVAL_SECONDS", gt=0, le=5
    )
    sse_heartbeat_seconds: float = Field(default=15.0, alias="SSE_HEARTBEAT_SECONDS", gt=0, le=60)
    sse_max_stream_seconds: float = Field(
        default=1200.0, alias="SSE_MAX_STREAM_SECONDS", gt=0, le=7200
    )

    jwt_secret: SecretStr = Field(alias="JWT_SECRET")
    jwt_issuer: str = Field(default="eaios-api", alias="JWT_ISSUER")
    jwt_audience: str = Field(default="eaios-web", alias="JWT_AUDIENCE")
    access_token_ttl_seconds: int = Field(
        default=900, alias="ACCESS_TOKEN_TTL_SECONDS", ge=60, le=3600
    )

    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"],
        alias="CORS_ALLOWED_ORIGINS",
    )

    login_max_failures_per_identity: int = Field(
        default=5, alias="LOGIN_MAX_FAILURES_PER_IDENTITY", ge=1
    )
    login_max_failures_per_client: int = Field(
        default=20, alias="LOGIN_MAX_FAILURES_PER_CLIENT", ge=1
    )
    login_failure_window_seconds: int = Field(
        default=900, alias="LOGIN_FAILURE_WINDOW_SECONDS", ge=1
    )
    # Argon2id verification costs ~64 MiB each. Measured in the 384 MiB API
    # container: ~68 MiB baseline + 64 MiB per concurrent verification, so 2
    # slots peak near 196 MiB. Applies per API process (uvicorn runs one).
    password_verify_concurrency: int = Field(
        default=2, alias="PASSWORD_VERIFY_CONCURRENCY", ge=1, le=16
    )
    password_verify_queue_timeout_seconds: float = Field(
        default=5.0, alias="PASSWORD_VERIFY_QUEUE_TIMEOUT_SECONDS", gt=0, le=30
    )
    rate_limit_key_prefix: str = Field(default="eaios:ratelimit", alias="RATE_LIMIT_KEY_PREFIX")

    readiness_timeout_seconds: float = Field(
        default=2.0, alias="READINESS_TIMEOUT_SECONDS", gt=0, le=10
    )

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("cors_allowed_origins")
    @classmethod
    def _no_wildcard(cls, value: list[str]) -> list[str]:
        if any(origin == "*" for origin in value):
            raise ValueError("CORS origins must be listed explicitly; '*' is not allowed")
        return value

    @field_validator("jwt_secret")
    @classmethod
    def _secret_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes")
        return value

    @field_validator("model_gateway_url")
    @classmethod
    def _gateway_root(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        root = value.strip().rstrip("/")
        if not root.startswith(("http://", "https://")):
            raise ValueError("LLM_BASE_URL must be an http(s) gateway root")
        if root.endswith("/v1"):
            raise ValueError(
                "LLM_BASE_URL is the gateway root; remove the trailing /v1 "
                "(MODEL_CHAT_PATH already starts with /v1)"
            )
        return root

    @field_validator(
        "model_chat_path",
        "model_embed_path",
        "model_check_path",
        "model_health_path",
        "model_parse_path",
    )
    @classmethod
    def _path(cls, value: str) -> str:
        if not value.startswith("/") or "://" in value or ".." in value:
            raise ValueError("model gateway paths must be absolute paths such as /check")
        return value

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if (
            self.relevance_filter is RelevanceFilter.DENSE_SCORE
            and self.retrieval_mode is not RetrievalMode.DENSE
        ):
            # RRF fusion scores are ranks, not cosine similarities.
            raise ValueError("RELEVANCE_FILTER=dense_score requires RETRIEVAL_MODE=dense")
        if self.environment is Environment.PRODUCTION:
            secret = self.jwt_secret.get_secret_value().lower()
            if any(marker in secret for marker in _PLACEHOLDER_MARKERS):
                raise ValueError("JWT_SECRET looks like a placeholder; refusing production start")
            if self.model_mode is ModelMode.FAKE:
                raise ValueError("MODEL_MODE=fake is development/test only")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment
