"""SQLAlchemy Core storage for Shoppie's support requests and agent runs."""

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import (
    Column,
    Engine,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    create_engine,
    delete,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.pool import NullPool

from backend.models import Calculation, CalculationStep

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "customer_orders.json"
SECURITY_DATA_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "security"
    / "customer_orders.json"
)
DEFAULT_DATABASE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "shoppie.sqlite3"
)
SCHEMA = "undercover"
MAX_POOL_SIZE = 2
STALE_WORK_SECONDS = 300
STALE_WORK_REASON = "The agent stopped answering"
MAX_OVERFLOW = 2
POOLER_PORT = 6432
MAX_ATTEMPTS = 2
STEP_MESSAGES = {
    "started": "The Shoppie Agent started reviewing this request.",
    "security_check": (
        "The security check screened the customer request for hidden instructions."
    ),
    "reading_request": "The agent retrieved the customer request and order details.",
    "checking_policy": "The agent checked the applicable Shoppie policy.",
    "calculated_refund": "The agent calculated the eligible refund amount.",
    "preparing_recommendation": (
        "The agent prepared a recommendation for support review."
    ),
    "sent_back": "A support reviewer sent the recommendation back with a note.",
    "signed_off": "A support agent reviewed and signed off on the request.",
    "completed": "The Shoppie Agent recorded the confirmed resolution.",
    "error": "This attempt failed; the request is back in the queue to be tried again.",
    "left_for_support": (
        "The agent could not complete its review after two attempts; "
        "support review is needed."
    ),
    "refusal": "A tool call was refused.",
}
AGENT_STAGES = {
    "security_check",
    "reading_request",
    "checking_policy",
    "refusal",
    "tool_call",
    "tool_result",
    "lookup",
    "memory_search",
    "memory_save",
}
TodoStatus = Literal["to_do", "in_progress", "done", "left_for_support"]
FailureResult = Literal["waiting", "left_for_support"]

metadata = MetaData()

support_requests = Table(
    "support_requests",
    metadata,
    Column("request_id", Text, primary_key=True),
    Column("order_id", Text, nullable=False),
    Column("customer_id", Text, nullable=False),
    Column("customer_name", Text, nullable=False),
    Column("request_type", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("source_status", Text, nullable=False),
    Column("queue_status", Text, nullable=False),
    Column("request_json", Text, nullable=False),
    Column("order_json", Text, nullable=False),
    Column("recommendation_json", Text),
    Column("review_json", Text),
    Column("sent_back_json", Text),
    Column("resume_heartbeat_at", Text),
    Column("attempts", Integer, nullable=False, default=0),
)

request_steps = Table(
    "request_steps",
    metadata,
    Column("step_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "request_id",
        Text,
        ForeignKey("support_requests.request_id"),
        nullable=False,
    ),
    Column("stage", Text, nullable=False),
    Column("message", Text, nullable=False),
    Column("sources", Text, nullable=True),
    Column("created_at", Text, nullable=False),
)

runs = Table(
    "runs",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("cap", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("started_at", Text, nullable=False),
    Column("finished_at", Text),
    Column("summary", Text),
    Column("last_heartbeat_at", Text),
    Column("stop_reason", Text),
)

work_gate = Table(
    "work_gate",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("active_run_id", Text),
)

run_todos = Table(
    "run_todos",
    metadata,
    Column("run_id", Text, ForeignKey("runs.run_id"), nullable=False),
    Column("position", Integer, nullable=False),
    Column("request_id", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("status", Text, nullable=False),
    PrimaryKeyConstraint("run_id", "request_id"),
)

calculations = Table(
    "calculations",
    metadata,
    Column(
        "request_id",
        Text,
        ForeignKey("support_requests.request_id"),
        primary_key=True,
    ),
    Column("attempt", Integer, nullable=False, default=0),
    Column("lines", Text, nullable=False),
    Column("payout", Float, nullable=False),
    Column("calculated_at", Text, nullable=False),
)


def now() -> str:
    """Return the current UTC time as ISO text."""
    return datetime.now(UTC).isoformat()


def sqlalchemy_postgres_url(url: str) -> str:
    """Convert a standard Postgres URL to SQLAlchemy's psycopg dialect URL."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def build_engine(
    database_path: Path = DEFAULT_DATABASE_PATH, database_url: str | None = None
) -> tuple[Engine, bool]:
    """Create a SQLAlchemy engine for Postgres (with undercover schema) or SQLite."""
    if database_url:
        options = f"-c search_path={SCHEMA}"
        connect_args: dict[str, Any] = {"options": options}
        uses_pooler = urlparse(database_url).port == POOLER_PORT
        if os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
            connect_args["prepare_threshold"] = None
            engine = create_engine(
                sqlalchemy_postgres_url(database_url),
                poolclass=NullPool,
                connect_args=connect_args,
            )
            return engine, True
        if uses_pooler:
            connect_args["prepare_threshold"] = None
        engine = create_engine(
            sqlalchemy_postgres_url(database_url),
            pool_size=MAX_POOL_SIZE,
            max_overflow=MAX_OVERFLOW,
            connect_args=connect_args,
        )
        return engine, True
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{database_path}"), False


class Store:
    """Read and update support requests and agent runs in SQLite or Postgres."""

    def __init__(
        self,
        database_path: Path | None = None,
        database_url: str | None = None,
    ) -> None:
        if database_url is None and database_path is None:
            env_path = os.getenv("SHOPPIE_DATABASE_PATH")
            env_url = os.getenv("DATABASE_URL")
            if env_url and not env_path:
                database_url = env_url
            else:
                database_path = Path(env_path) if env_path else DEFAULT_DATABASE_PATH
        self.database_path = database_path or DEFAULT_DATABASE_PATH
        self.database_url = database_url
        self.engine, self.is_postgres = build_engine(
            self.database_path, self.database_url
        )

    def initialize(self) -> None:
        """Create the tables, seed source records once and recover stopped work."""
        customers = list(json.loads(DATA_PATH.read_text(encoding="utf-8"))["customers"])
        if SECURITY_DATA_PATH.is_file():
            customers.extend(
                json.loads(SECURITY_DATA_PATH.read_text(encoding="utf-8"))["customers"]
            )
        if self.is_postgres:
            with self.engine.begin() as connection:
                connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            self._ensure_schema_columns(connection)
            gate_row = connection.execute(
                select(work_gate.c.id).where(work_gate.c.id == 1)
            ).first()
            if gate_row is None:
                active = connection.execute(
                    select(runs.c.run_id)
                    .where(runs.c.status == "running")
                    .order_by(runs.c.started_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                connection.execute(insert(work_gate).values(id=1, active_run_id=active))
            for customer in customers:
                for order in customer["orders"]:
                    safe_order = self.safe_order(order)
                    for request in order["requests"]:
                        self._upsert_seed_request(
                            connection, customer, order, request, safe_order
                        )

    @staticmethod
    def _ensure_schema_columns(connection: Any) -> None:
        inspector = inspect(connection)
        req_cols = {col["name"] for col in inspector.get_columns("support_requests")}
        for col_name in ("sent_back_json", "resume_heartbeat_at"):
            if col_name not in req_cols:
                connection.execute(
                    text(f"ALTER TABLE support_requests ADD COLUMN {col_name} TEXT")
                )
        run_cols = {col["name"] for col in inspector.get_columns("runs")}
        for col_name in ("last_heartbeat_at", "stop_reason"):
            if col_name not in run_cols:
                connection.execute(text(f"ALTER TABLE runs ADD COLUMN {col_name} TEXT"))

    def _upsert_seed_request(
        self,
        connection: Any,
        customer: dict[str, Any],
        order: dict[str, Any],
        request: dict[str, Any],
        safe_order: dict[str, Any],
    ) -> None:
        safe_request = self.safe_request(request)
        existing = connection.execute(
            select(support_requests.c.request_id).where(
                support_requests.c.request_id == request["request_id"]
            )
        ).first()
        if existing is None:
            connection.execute(
                insert(support_requests).values(
                    request_id=request["request_id"],
                    order_id=order["order_id"],
                    customer_id=customer["customer_id"],
                    customer_name=customer["name"],
                    request_type=request["type"],
                    created_at=request["created_at"],
                    source_status=request["status"],
                    queue_status="waiting",
                    request_json=json.dumps(safe_request),
                    order_json=json.dumps(safe_order),
                    attempts=0,
                )
            )
        else:
            connection.execute(
                update(support_requests)
                .where(support_requests.c.request_id == request["request_id"])
                .values(
                    request_json=json.dumps(safe_request),
                    order_json=json.dumps(safe_order),
                )
            )

    def reseed(self) -> None:
        """Drop and rebuild all tables from the source records."""
        if self.is_postgres:
            with self.engine.begin() as connection:
                connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
                for table in (
                    "checkpoint_writes",
                    "checkpoint_blobs",
                    "checkpoints",
                    "checkpoint_migrations",
                ):
                    connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
            metadata.drop_all(self.engine)
        else:
            metadata.drop_all(self.engine)
            self.engine.dispose()
            self.database_path.unlink(missing_ok=True)
        self.initialize()

    @staticmethod
    def safe_request(request: dict[str, Any]) -> dict[str, Any]:
        """Select the customer-provided request fields."""
        return {
            "request_id": request["request_id"],
            "type": request["type"],
            "created_at": request["created_at"],
            "customer_message": request["customer_message"],
            "requested_scope": request["requested_scope"],
            "evidence": request["evidence"],
        }

    @staticmethod
    def safe_order(order: dict[str, Any]) -> dict[str, Any]:
        """Select order fields needed to review a customer request."""
        return {
            "order_id": order["order_id"],
            "placed_at": order["placed_at"],
            "fulfillment_status": order["fulfillment_status"],
            "fulfillment_events": order["fulfillment_events"],
            "shipment": order.get("shipment"),
            "payment_status": order["payment_status"],
            "payment_method": order["payment_method"],
            "delivery_window": order.get("delivery_window"),
            "delivered_at": order.get("delivered_at"),
            "items": order["items"],
            "subtotal": order["subtotal"],
            "delivery_fee": order["delivery_fee"],
            "total": order["total"],
        }

    def list_requests(self) -> list[dict[str, Any]]:
        """Return queue summaries ordered by creation time."""
        query = select(
            support_requests.c.request_id,
            support_requests.c.order_id,
            support_requests.c.customer_id,
            support_requests.c.customer_name,
            support_requests.c.request_type,
            support_requests.c.created_at,
            support_requests.c.queue_status,
            support_requests.c.recommendation_json,
            support_requests.c.review_json,
            support_requests.c.sent_back_json,
            support_requests.c.resume_heartbeat_at,
            support_requests.c.attempts,
        ).order_by(support_requests.c.created_at, support_requests.c.request_id)
        with self.engine.begin() as connection:
            rows = connection.execute(query).mappings().all()
        return [
            {
                "request_id": row["request_id"],
                "order_id": row["order_id"],
                "customer_id": row["customer_id"],
                "customer_name": row["customer_name"],
                "type": row["request_type"],
                "created_at": row["created_at"],
                "status": row["queue_status"],
                "attempts": row["attempts"],
                "recommendation": self.decode(row["recommendation_json"]),
                "review": self.decode(row["review_json"]),
                "sent_back": self.decode(row["sent_back_json"]) or [],
                "resume_heartbeat_at": row["resume_heartbeat_at"],
            }
            for row in rows
        ]

    def list_waiting(self) -> list[dict[str, str]]:
        """Return the id, type and creation time of each waiting or sent-back item."""
        query = (
            select(
                support_requests.c.request_id,
                support_requests.c.request_type,
                support_requests.c.created_at,
            )
            .where(support_requests.c.queue_status.in_(["waiting", "sent_back"]))
            .order_by(support_requests.c.created_at, support_requests.c.request_id)
        )
        with self.engine.begin() as connection:
            rows = connection.execute(query).mappings().all()
        return [
            {
                "request_id": row["request_id"],
                "type": row["request_type"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        """Return a request, safe customer/order details, and its progress."""
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(support_requests).where(
                        support_requests.c.request_id == request_id
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            step_rows = (
                connection.execute(
                    select(
                        request_steps.c.stage,
                        request_steps.c.message,
                        request_steps.c.created_at,
                        request_steps.c.sources,
                    )
                    .where(request_steps.c.request_id == request_id)
                    .order_by(request_steps.c.step_id)
                )
                .mappings()
                .all()
            )
        formatted_steps: list[dict[str, Any]] = []
        for step in step_rows:
            s = dict(step)
            raw_sources = s.pop("sources", None)
            if raw_sources:
                try:
                    s["sources"] = json.loads(raw_sources)
                except Exception:
                    s["sources"] = []
            else:
                s["sources"] = []
            formatted_steps.append(s)
        return {
            "request": json.loads(row["request_json"]),
            "customer": {
                "customer_id": row["customer_id"],
                "name": row["customer_name"],
            },
            "order": json.loads(row["order_json"]),
            "status": row["queue_status"],
            "attempts": row["attempts"],
            "recommendation": self.decode(row["recommendation_json"]),
            "review": self.decode(row["review_json"]),
            "sent_back": self.decode(row["sent_back_json"]) or [],
            "resume_heartbeat_at": row["resume_heartbeat_at"],
            "steps": formatted_steps,
        }

    def start_request(self, request_id: str) -> int | None:
        """Move a waiting request into progress and return its attempt number."""
        with self.engine.begin() as connection:
            result = connection.execute(
                update(support_requests)
                .where(
                    support_requests.c.request_id == request_id,
                    support_requests.c.queue_status == "waiting",
                )
                .values(queue_status="in_progress")
            )
            if result.rowcount != 1:
                return None
            attempts = int(
                connection.execute(
                    select(support_requests.c.attempts).where(
                        support_requests.c.request_id == request_id
                    )
                ).scalar_one()
            )
            if attempts == 0:
                connection.execute(
                    delete(request_steps).where(
                        request_steps.c.request_id == request_id
                    )
                )
            connection.execute(
                delete(calculations).where(calculations.c.request_id == request_id)
            )
            attempt = attempts + 1
            self._insert_step(
                connection,
                request_id,
                "started",
                f"{STEP_MESSAGES['started']} Attempt {attempt} of {MAX_ATTEMPTS}.",
            )
        return attempt

    def is_in_progress(self, request_id: str) -> bool:
        """Report whether the agent is working on a request."""
        with self.engine.begin() as connection:
            status = connection.execute(
                select(support_requests.c.queue_status).where(
                    support_requests.c.request_id == request_id
                )
            ).scalar_one_or_none()
        return status == "in_progress"

    def add_step(
        self,
        request_id: str,
        stage: str,
        message: str = "",
        sources: list[dict[str, str]] | None = None,
    ) -> bool:
        """Record a progress step while a request is running."""
        with self.engine.begin() as connection:
            status = connection.execute(
                select(support_requests.c.queue_status).where(
                    support_requests.c.request_id == request_id
                )
            ).scalar_one_or_none()
            if status != "in_progress":
                return False
            self._insert_step(connection, request_id, stage, message, sources)
        return True

    def keep_calculation(
        self,
        request_id: str,
        attempt: int,
        lines: Sequence[Any],
        payout: float,
    ) -> None:
        """Keep a request's latest calculation, replacing any before it."""
        serialized = [
            line.model_dump()
            if hasattr(line, "model_dump")
            else dict(line)
            if isinstance(line, dict)
            else line
            for line in lines
        ]
        with self.engine.begin() as connection:
            connection.execute(
                delete(calculations).where(calculations.c.request_id == request_id)
            )
            connection.execute(
                insert(calculations).values(
                    request_id=request_id,
                    attempt=attempt,
                    lines=json.dumps(serialized),
                    payout=payout,
                    calculated_at=now(),
                )
            )

    def attempts_calculation(
        self, request_id: str, attempt: int | None = None
    ) -> Calculation | None:
        """Return the latest calculation on a request's attempt, if any."""
        with self.engine.begin() as connection:
            if attempt is None:
                row_att = connection.execute(
                    select(support_requests.c.attempts).where(
                        support_requests.c.request_id == request_id
                    )
                ).scalar_one_or_none()
                if row_att is None:
                    return None
                attempt = int(row_att) + 1
            query = select(calculations).where(
                calculations.c.request_id == request_id,
                calculations.c.attempt == attempt,
            )
            row = connection.execute(query).mappings().first()
            if not row:
                return None
            raw_lines = (
                json.loads(row["lines"])
                if isinstance(row["lines"], str)
                else row["lines"]
            )
            steps = [
                CalculationStep(step=line["step"], amount=float(line["amount"]))
                for line in raw_lines
            ]
            return Calculation(lines=steps, payout=float(row["payout"]))

    def save_recommendation(
        self, request_id: str, recommendation: dict[str, Any]
    ) -> bool:
        """Persist an agent recommendation once; treat an identical repeat as done."""
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(
                        support_requests.c.queue_status,
                        support_requests.c.recommendation_json,
                    ).where(support_requests.c.request_id == request_id)
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            existing = self.decode(row["recommendation_json"])
            if existing is not None:
                return existing == recommendation
            if row["queue_status"] != "in_progress":
                return False
            connection.execute(
                update(support_requests)
                .where(support_requests.c.request_id == request_id)
                .values(
                    recommendation_json=json.dumps(recommendation),
                    resume_heartbeat_at=None,
                    queue_status="waiting_for_agent_review",
                )
            )
            self._insert_step(connection, request_id, "preparing_recommendation")
        return True

    def send_back_request(self, request_id: str, note: str, sent_back_by: str) -> bool:
        """Archive the recommendation with the reviewer note and mark sent_back."""
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(
                        support_requests.c.queue_status,
                        support_requests.c.recommendation_json,
                        support_requests.c.sent_back_json,
                    ).where(support_requests.c.request_id == request_id)
                )
                .mappings()
                .first()
            )
            if row is None or row["queue_status"] != "waiting_for_agent_review":
                return False
            existing_rec = self.decode(row["recommendation_json"])
            if existing_rec is None:
                return False
            history = self.decode(row["sent_back_json"]) or []
            history.append(
                {
                    "decision": existing_rec,
                    "note": note,
                    "sent_back_by": sent_back_by,
                    "sent_back_at": now(),
                }
            )
            result = connection.execute(
                update(support_requests)
                .where(
                    support_requests.c.request_id == request_id,
                    support_requests.c.queue_status == "waiting_for_agent_review",
                )
                .values(
                    sent_back_json=json.dumps(history),
                    recommendation_json=None,
                    review_json=None,
                    resume_heartbeat_at=None,
                    queue_status="sent_back",
                )
            )
            if result.rowcount != 1:
                return False
            connection.execute(
                delete(calculations).where(calculations.c.request_id == request_id)
            )
            self._insert_step(
                connection,
                request_id,
                "sent_back",
                f"Sent back by {sent_back_by}: {note}",
            )
        return True

    def take_sent_back(
        self, request_id: str, owner: Literal["resume", "run"]
    ) -> int | None:
        """Atomically move a sent_back request into progress for the first caller."""
        heartbeat = now() if owner == "resume" else None
        with self.engine.begin() as connection:
            result = connection.execute(
                update(support_requests)
                .where(
                    support_requests.c.request_id == request_id,
                    support_requests.c.queue_status == "sent_back",
                )
                .values(
                    queue_status="in_progress",
                    resume_heartbeat_at=heartbeat,
                )
            )
            if result.rowcount != 1:
                return None
            attempts = int(
                connection.execute(
                    select(support_requests.c.attempts).where(
                        support_requests.c.request_id == request_id
                    )
                ).scalar_one()
            )
        return attempts + 1

    def heartbeat_request(self, request_id: str) -> bool:
        """Record a live resume heartbeat on an in-progress request."""
        with self.engine.begin() as connection:
            result = connection.execute(
                update(support_requests)
                .where(
                    support_requests.c.request_id == request_id,
                    support_requests.c.queue_status == "in_progress",
                )
                .values(resume_heartbeat_at=now())
            )
        return result.rowcount == 1

    def hold_request(self, request_id: str, reason: str) -> bool:
        """Hold an approved request whose payment was refused by the Gateway."""
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(
                        support_requests.c.queue_status,
                        support_requests.c.recommendation_json,
                    ).where(support_requests.c.request_id == request_id)
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            rec = self.decode(row["recommendation_json"]) or {}
            rec["gateway_refusal"] = reason
            connection.execute(
                update(support_requests)
                .where(support_requests.c.request_id == request_id)
                .values(
                    recommendation_json=json.dumps(rec),
                    review_json=None,
                    queue_status="waiting_for_agent_review",
                )
            )
            self._insert_step(
                connection,
                request_id,
                "refusal",
                f"Payment refused by the Gateway: {reason}",
            )
        return True

    def fail_request(self, request_id: str) -> FailureResult | None:
        """Count a failed attempt: back to the queue once, then left for support."""
        with self.engine.begin() as connection:
            attempts = connection.execute(
                select(support_requests.c.attempts).where(
                    support_requests.c.request_id == request_id,
                    support_requests.c.queue_status == "in_progress",
                )
            ).scalar_one_or_none()
            if attempts is None:
                return None
            next_attempts = int(attempts) + 1
            exhausted = next_attempts >= MAX_ATTEMPTS
            connection.execute(
                update(support_requests)
                .where(support_requests.c.request_id == request_id)
                .values(
                    attempts=next_attempts,
                    queue_status="waiting_for_agent_review" if exhausted else "waiting",
                    recommendation_json=None,
                    resume_heartbeat_at=None,
                )
            )
            self._insert_step(
                connection, request_id, "left_for_support" if exhausted else "error"
            )
        return "left_for_support" if exhausted else "waiting"

    def record_review(self, request_id: str, review: dict[str, Any]) -> str | None:
        """Store a support-agent sign-off once; return the resulting queue status."""
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(
                        support_requests.c.queue_status,
                        support_requests.c.recommendation_json,
                        support_requests.c.review_json,
                    ).where(support_requests.c.request_id == request_id)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            existing = self.decode(row["review_json"])
            if existing is not None:
                if row["queue_status"] == "decided" and self._reviews_match(
                    existing, review
                ):
                    return str(row["queue_status"])
                return None
            if row["queue_status"] != "waiting_for_agent_review":
                return None
            has_recommendation = row["recommendation_json"] is not None
            result = connection.execute(
                update(support_requests)
                .where(
                    support_requests.c.request_id == request_id,
                    support_requests.c.queue_status == "waiting_for_agent_review",
                )
                .values(
                    review_json=json.dumps(review),
                    queue_status="decided",
                )
            )
            if result.rowcount != 1:
                return None
            self._insert_step(
                connection,
                request_id,
                "signed_off",
                f"Signed off ({review['outcome']}) by {review['reviewed_by']}.",
            )
            if has_recommendation:
                self._insert_step(
                    connection,
                    request_id,
                    "completed",
                    f"Confirmed {review['outcome']} after review by "
                    f"{review['reviewed_by']}.",
                )
        return "decided"

    def complete_request(self, request_id: str, review: dict[str, Any]) -> bool:
        """Record the confirmed resolution once and mark the request decided."""
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(
                        support_requests.c.queue_status,
                        support_requests.c.review_json,
                    ).where(support_requests.c.request_id == request_id)
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            existing = self.decode(row["review_json"])
            if row["queue_status"] == "decided":
                return existing is not None and self._reviews_match(existing, review)
            if row["queue_status"] not in {"in_progress", "waiting_for_agent_review"}:
                return False
            if existing is not None and not self._reviews_match(existing, review):
                return False
            connection.execute(
                update(support_requests)
                .where(support_requests.c.request_id == request_id)
                .values(
                    review_json=json.dumps(review),
                    resume_heartbeat_at=None,
                    queue_status="decided",
                )
            )
            self._insert_step(
                connection,
                request_id,
                "completed",
                f"Confirmed {review['outcome']} after review by "
                f"{review['reviewed_by']}.",
            )
        return True

    @staticmethod
    def _reviews_match(existing: dict[str, Any], review: dict[str, Any]) -> bool:
        same_reviewer = (
            "reviewed_by" not in existing
            or "reviewed_by" not in review
            or existing.get("reviewed_by") == review.get("reviewed_by")
        )
        return (
            existing.get("outcome") == review.get("outcome")
            and existing.get("notes") == review.get("notes")
            and same_reviewer
        )

    @staticmethod
    def _is_stale_run(started_at: str, last_heartbeat_at: str | None) -> bool:
        latest = datetime.fromisoformat(started_at)
        if last_heartbeat_at:
            latest = max(latest, datetime.fromisoformat(last_heartbeat_at))
        return (datetime.now(UTC) - latest).total_seconds() >= STALE_WORK_SECONDS

    def _clear_stale_gate_if_needed(self, connection: Any) -> bool:
        """Lock work_gate row 1 and clear it if its run stopped or timed out."""
        connection.execute(
            update(work_gate)
            .where(work_gate.c.id == 1)
            .values(active_run_id=work_gate.c.active_run_id)
        )
        active_id = connection.execute(
            select(work_gate.c.active_run_id).where(work_gate.c.id == 1)
        ).scalar_one_or_none()
        if not active_id:
            return True
        row = (
            connection.execute(
                select(
                    runs.c.run_id,
                    runs.c.status,
                    runs.c.started_at,
                    runs.c.last_heartbeat_at,
                ).where(runs.c.run_id == active_id)
            )
            .mappings()
            .first()
        )
        if row is not None and row["status"] == "running":
            if not self._is_stale_run(row["started_at"], row["last_heartbeat_at"]):
                return False
            connection.execute(
                update(runs)
                .where(runs.c.run_id == active_id, runs.c.status == "running")
                .values(
                    status="failed",
                    finished_at=now(),
                    summary=STALE_WORK_REASON,
                    stop_reason=STALE_WORK_REASON,
                )
            )
        connection.execute(
            update(work_gate).where(work_gate.c.id == 1).values(active_run_id=None)
        )
        return True

    def create_run(self, cap: int) -> str | None:
        """Atomically claim the work gate and commit a new running run."""
        run_id = f"RUN-{uuid4().hex[:8].upper()}"
        with self.engine.begin() as connection:
            if not self._clear_stale_gate_if_needed(connection):
                return None
            claimed = connection.execute(
                update(work_gate)
                .where(work_gate.c.id == 1, work_gate.c.active_run_id.is_(None))
                .values(active_run_id=run_id)
            )
            if claimed.rowcount != 1:
                return None
            connection.execute(
                insert(runs).values(
                    run_id=run_id,
                    cap=cap,
                    status="running",
                    started_at=now(),
                )
            )
        return run_id

    def active_run_id(self) -> str | None:
        """Return the currently gated run_id when a run holds the lock."""
        with self.engine.begin() as connection:
            return connection.execute(
                select(work_gate.c.active_run_id).where(work_gate.c.id == 1)
            ).scalar_one_or_none()

    def finish_run(
        self,
        run_id: str,
        status: str,
        summary: str,
        stop_reason: str | None = None,
    ) -> None:
        """Close one run with its summary and release the work gate if it holds it."""
        values: dict[str, Any] = {
            "status": status,
            "summary": summary,
            "finished_at": now(),
            "stop_reason": stop_reason,
        }
        with self.engine.begin() as connection:
            connection.execute(
                update(runs).where(runs.c.run_id == run_id).values(**values)
            )
            connection.execute(
                update(work_gate)
                .where(work_gate.c.id == 1, work_gate.c.active_run_id == run_id)
                .values(active_run_id=None)
            )

    def heartbeat_run(self, run_id: str) -> bool:
        """Record a live heartbeat for a running run."""
        with self.engine.begin() as connection:
            result = connection.execute(
                update(runs)
                .where(runs.c.run_id == run_id, runs.c.status == "running")
                .values(last_heartbeat_at=now())
            )
            return result.rowcount == 1

    def is_running(self, run_id: str) -> bool:
        """Report whether a run is still running."""
        with self.engine.begin() as connection:
            status = connection.execute(
                select(runs.c.status).where(runs.c.run_id == run_id)
            ).scalar_one_or_none()
        return status == "running"

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return one run with its to-do list, or None if not found."""
        with self.engine.begin() as connection:
            run = (
                connection.execute(select(runs).where(runs.c.run_id == run_id))
                .mappings()
                .first()
            )
            return self._attach_todos(connection, run)

    def current_run(self) -> dict[str, Any] | None:
        """Return the latest run with its to-do list."""
        with self.engine.begin() as connection:
            run = (
                connection.execute(
                    select(runs).order_by(runs.c.started_at.desc()).limit(1)
                )
                .mappings()
                .first()
            )
            return self._attach_todos(connection, run)

    @staticmethod
    def _attach_todos(connection: Any, run: Any) -> dict[str, Any] | None:
        if run is None:
            return None
        todos = (
            connection.execute(
                select(
                    run_todos.c.request_id,
                    run_todos.c.title,
                    run_todos.c.status,
                )
                .where(run_todos.c.run_id == run["run_id"])
                .order_by(run_todos.c.position)
            )
            .mappings()
            .all()
        )
        return {**dict(run), "todos": [dict(todo) for todo in todos]}

    def write_todos(self, run_id: str, todos: list[dict[str, str]]) -> None:
        """Replace a run's to-do list."""
        with self.engine.begin() as connection:
            connection.execute(delete(run_todos).where(run_todos.c.run_id == run_id))
            if todos:
                connection.execute(
                    insert(run_todos),
                    [
                        {
                            "run_id": run_id,
                            "position": index,
                            "request_id": todo["request_id"],
                            "title": todo["title"],
                            "status": todo["status"],
                        }
                        for index, todo in enumerate(todos)
                    ],
                )

    def update_todo(self, run_id: str, request_id: str, status: TodoStatus) -> None:
        """Set one to-do item's status, adding the item if the plan missed it."""
        with self.engine.begin() as connection:
            result = connection.execute(
                update(run_todos)
                .where(
                    run_todos.c.run_id == run_id,
                    run_todos.c.request_id == request_id,
                )
                .values(status=status)
            )
            if result.rowcount == 0:
                max_pos = connection.execute(
                    select(func.coalesce(func.max(run_todos.c.position), -1)).where(
                        run_todos.c.run_id == run_id
                    )
                ).scalar_one()
                connection.execute(
                    insert(run_todos).values(
                        run_id=run_id,
                        position=int(max_pos) + 1,
                        request_id=request_id,
                        title=f"Work {request_id}",
                        status=status,
                    )
                )

    @staticmethod
    def _insert_step(
        connection: Any,
        request_id: str,
        stage: str,
        message: str = "",
        sources: list[dict[str, str]] | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "request_id": request_id,
            "stage": stage,
            "message": message or STEP_MESSAGES.get(stage, stage),
            "created_at": now(),
        }
        if sources is not None:
            values["sources"] = json.dumps(sources)
        connection.execute(insert(request_steps).values(**values))

    @staticmethod
    def decode(value: str | None) -> Any:
        """Decode stored JSON when a value exists."""
        return json.loads(value) if value else None
