"""The Shoppie support backend."""

import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from mangum import Mangum
from pydantic import BaseModel, Field, field_validator

from backend import agent_service
from backend.instructions import QUEUE_INSTRUCTIONS, REQUEST_INSTRUCTIONS
from backend.models import DecisionKind, DecisionRequest
from backend.policies import list_policies, read_section
from backend.records import check_calculation, check_request
from backend.refunds import (
    MAX_REFUND_LIMIT,
    RefundCalculation,
    RefundInput,
    requested_refund_total,
    work_out_refund,
)
from backend.store import AGENT_STAGES, Store, TodoStatus, now

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "shoppie.sqlite3"
)
DASHBOARD_DIST = Path(__file__).resolve().parent.parent / "dashboard" / "dist"
AGENT_OPERATION_IDS = {
    "get_request_context",
    "calculate_refund",
    "add_request_step",
    "start_request",
    "save_recommendation",
    "complete_request",
}


def database_path() -> Path:
    """Return the SQLite path from SHOPPIE_DATABASE_PATH or the default."""
    return Path(os.getenv("SHOPPIE_DATABASE_PATH", DEFAULT_DATABASE_PATH))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the database and seed the Shoppie queue."""
    store = Store()
    store.initialize()
    app.state.store = store
    yield


app = FastAPI(title="Shoppie", lifespan=lifespan)


def dashboard_file(url_path: str) -> Path | None:
    """Resolve a static file or SPA index.html from the built dashboard."""
    if not DASHBOARD_DIST.is_dir():
        return None
    root = DASHBOARD_DIST.resolve()
    relative = url_path.lstrip("/")
    if relative:
        candidate = (root / relative).resolve()
        if root in candidate.parents and candidate.is_file():
            return candidate
    index = root / "index.html"
    return index if index.is_file() else None


@app.middleware("http")
async def serve_dashboard(request: Request, call_next: Any) -> Any:
    """Serve the built React dashboard outside /api when dashboard/dist exists."""
    path = request.url.path
    if (
        request.method in {"GET", "HEAD"}
        and path != "/api"
        and not path.startswith("/api/")
    ):
        target = dashboard_file(path)
        if target is not None:
            return FileResponse(target)
    return await call_next(request)


class ProgressInput(BaseModel):
    """A supported progress stage supplied by the agent."""

    stage: str
    message: str = ""
    sources: list[dict[str, str]] = []


class ReviewInput(BaseModel):
    """A support agent's reviewed recommendation."""

    outcome: Literal["approve", "decline", "request_information"]
    notes: str
    reviewed_by: str = Field(min_length=1)


class SendBackInput(BaseModel):
    """A support reviewer's note sending a decision back to the agent."""

    note: str = Field(min_length=1, max_length=1000)
    sent_back_by: str = Field(min_length=1)

    @field_validator("note", "sent_back_by")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        """Require non-whitespace text on send-back fields."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("Field cannot be blank")
        return stripped


class TakeSentBackInput(BaseModel):
    """Which worker is claiming a sent-back request."""

    owner: Literal["resume", "run"]


class CompletionInput(BaseModel):
    """A confirmed review used by the agent to complete a request."""

    outcome: Literal["approve", "decline", "request_information"]
    notes: str
    reviewed_by: str
    reviewed_at: str


class HoldInput(BaseModel):
    """The reason a payment or resolution was refused."""

    reason: str


class RunInput(BaseModel):
    """The most requests a run may work."""

    cap: int = Field(ge=1)


class TodoItem(BaseModel):
    """One item on a run's to-do list."""

    request_id: str
    title: str
    status: TodoStatus


class TodoList(BaseModel):
    """A run's whole to-do list."""

    todos: list[TodoItem]


class TodoStatusInput(BaseModel):
    """A new status for one to-do item."""

    status: TodoStatus


class RunFinishInput(BaseModel):
    """How a run ended and the queue agent's summary."""

    status: Literal["finished", "failed"]
    summary: str


def store_of(request: Request) -> Store:
    """Return the app's store, creating it lazily when lifespan has not run."""
    store = getattr(request.app.state, "store", None)
    if store is None:
        store = Store()
        request.app.state.store = store
    return store


def has_valid_agent_key(request: Request) -> bool:
    """Return whether the request carries the configured UNDERCOVER_API_KEY."""
    expected = os.getenv("UNDERCOVER_API_KEY")
    supplied = request.headers.get("X-API-Key")
    if not expected or not supplied:
        return False
    return secrets.compare_digest(supplied, expected)


def require_agent_key(request: Request) -> None:
    """Refuse requests that do not carry the valid agent X-API-Key."""
    if not has_valid_agent_key(request):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_shared_read(request: Request) -> None:
    """Allow dashboard reads or valid agent X-API-Key reads."""
    if "X-API-Key" in request.headers:
        require_agent_key(request)


def require_support_handler(request: Request) -> None:
    """Refuse support handler actions authenticated only with the agent X-API-Key."""
    if "X-API-Key" in request.headers:
        raise HTTPException(
            status_code=401,
            detail="Agent API key cannot stand in for a support handler",
        )


def require_request(store: Store, request_id: str) -> dict[str, Any]:
    """Return a request or raise 404."""
    result = store.get_request(request_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Support request not found")
    return result


def run_in_background(store: Store, run_id: str, cap: int) -> None:
    """Hand a run to the agent entrypoint, failing only if it did not take it."""
    result = agent_service.call_agent(
        {"operation": "work_run", "run_id": run_id, "cap": cap}
    )
    if result.state in ("did_not_take", "refused"):
        store.finish_run(
            run_id,
            "failed",
            f"The run stopped: the agent service failed ({result.reason})",
        )
    elif result.state == "uncertain":
        logger.warning(
            "Uncertain whether the agent started run %s: %s", run_id, result.reason
        )


def notify_agent_resume(request_id: str) -> None:
    """Ask the agent entrypoint to resume a paused request, warning if not taken."""
    result = agent_service.call_agent(
        {"operation": "resume_request", "request_id": request_id}
    )
    if result.state != "accepted":
        logger.warning(
            "Could not reach agent service to resume %s (%s): %s",
            request_id,
            result.state,
            result.reason,
        )


@app.get("/api/health", operation_id="health")
def health() -> dict[str, str]:
    """Check that the backend is running."""
    return {"status": "ok"}


@app.get(
    "/api/requests",
    operation_id="list_requests",
    dependencies=[Depends(require_shared_read)],
)
def list_requests(request: Request) -> list[dict[str, Any]]:
    """List support requests in queue order."""
    return store_of(request).list_requests()


@app.get(
    "/api/requests/waiting",
    operation_id="list_waiting_requests",
    dependencies=[Depends(require_agent_key)],
)
def list_waiting_requests(request: Request) -> list[dict[str, str]]:
    """List the id, type and creation time of each waiting request."""
    return store_of(request).list_waiting()


@app.get(
    "/api/requests/{request_id}",
    operation_id="get_request",
    dependencies=[Depends(require_shared_read)],
)
def get_request(request_id: str, request: Request) -> dict[str, Any]:
    """Return a support request and its related records."""
    return require_request(store_of(request), request_id)


@app.get(
    "/api/requests/{request_id}/context",
    operation_id="get_request_context",
    dependencies=[Depends(require_agent_key)],
)
def get_request_context(request_id: str, request: Request) -> dict[str, Any]:
    """Return the request, customer and order context available to the agent."""
    return require_request(store_of(request), request_id)


@app.get(
    "/api/policies",
    operation_id="list_policy_sections",
    dependencies=[Depends(require_agent_key)],
)
def list_policy_sections() -> list[dict[str, object]]:
    """List Shoppie's policies and the sections in each."""
    return list_policies()


@app.get(
    "/api/requests/{request_id}/policies/{policy}/sections/{section}",
    operation_id="read_policy_section",
    dependencies=[Depends(require_agent_key)],
)
def read_policy_section(
    request_id: str, policy: str, section: str, request: Request
) -> dict[str, str]:
    """Return one policy section and record on the request that the agent read it."""
    store = store_of(request)
    require_request(store, request_id)
    result = read_section(policy, section)
    if result is None:
        raise HTTPException(status_code=404, detail="Policy section not found")
    message = f"Read {result['name']}, {result['title']}."
    if not store.add_step(request_id, "read_section", message):
        raise HTTPException(status_code=409, detail="Request is not in progress")
    return result


@app.get(
    "/api/agent/instructions",
    operation_id="get_agent_instructions",
    dependencies=[Depends(require_shared_read)],
)
def get_agent_instructions() -> dict[str, str]:
    """Return the read-only instructions for running the queue and working a request."""
    return {"queue": QUEUE_INSTRUCTIONS, "request": REQUEST_INSTRUCTIONS}


@app.get(
    "/api/agent/openapi.json",
    operation_id="get_agent_openapi",
    dependencies=[Depends(require_shared_read)],
)
def get_agent_openapi() -> dict[str, Any]:
    """Return the OpenAPI schema containing only the request agent's operations."""
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.operation_id in AGENT_OPERATION_IDS
    ]
    schema = get_openapi(title=app.title, version=app.version, routes=routes)
    schema.pop("servers", None)
    return schema


@app.post(
    "/api/requests/{request_id}/steps",
    operation_id="add_request_step",
    dependencies=[Depends(require_agent_key)],
)
def add_request_step(
    request_id: str, progress: ProgressInput, request: Request
) -> dict[str, str]:
    """Record a supported agent progress step."""
    if progress.stage not in AGENT_STAGES:
        raise HTTPException(status_code=422, detail="Unsupported progress stage")
    if not store_of(request).add_step(
        request_id, progress.stage, progress.message, progress.sources
    ):
        raise HTTPException(status_code=409, detail="Request is not in progress")
    return {"status": "recorded", "stage": progress.stage}


@app.post(
    "/api/requests/{request_id}/refund",
    operation_id="calculate_refund",
    dependencies=[Depends(require_agent_key)],
)
def calculate_refund(
    request_id: str, refund: RefundInput, request: Request
) -> RefundCalculation:
    """Calculate the policy-backed refund for a request in progress."""
    store = store_of(request)
    record = require_request(store, request_id)
    try:
        requested = requested_refund_total(record["order"], refund.lines)
        result = work_out_refund(record["order"], refund.reason, refund.lines)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if max(requested, result["refund"]) > MAX_REFUND_LIMIT:
        raise HTTPException(
            status_code=403,
            detail=f"Refund exceeds the ₹{MAX_REFUND_LIMIT:,.2f} approval limit",
        )
    if not store.is_in_progress(request_id):
        raise HTTPException(status_code=409, detail="Request is not in progress")
    attempt = int(record.get("attempts", 0)) + 1
    store.keep_calculation(request_id, attempt, result["calculation"], result["refund"])
    store.add_step(
        request_id,
        "calculated_refund",
        f"Calculated eligible refund of ₹{result['refund']:,.2f}.",
    )
    return result


@app.post(
    "/api/requests/{request_id}/start",
    operation_id="start_request",
    dependencies=[Depends(require_agent_key)],
)
def start_request(request_id: str, request: Request) -> dict[str, Any]:
    """Move a waiting request into progress for the agent."""
    store = store_of(request)
    require_request(store, request_id)
    attempt = store.start_request(request_id)
    if attempt is None:
        raise HTTPException(status_code=409, detail="Request is not waiting")
    return {"request_id": request_id, "status": "in_progress", "attempt": attempt}


@app.post(
    "/api/requests/{request_id}/recommendation",
    operation_id="save_recommendation",
    dependencies=[Depends(require_agent_key)],
)
def save_recommendation(
    request_id: str, recommendation: DecisionRequest, request: Request
) -> dict[str, Any]:
    """Store the agent's one recommendation for support review."""
    check_request(recommendation)
    store = store_of(request)
    record = require_request(store, request_id)
    existing = record.get("recommendation")
    if existing is not None:
        same_decision = (
            existing.get("decision") == recommendation.decision.value
            and existing.get("payout") == recommendation.payout
        )
        same_outcome = (
            existing.get("outcome") == recommendation.decision.value
            and existing.get("refund_amount") == recommendation.payout
        )
        if (
            same_decision
            or same_outcome
            or existing.get("payout") == recommendation.payout
        ):
            return {
                "request_id": request_id,
                "status": record["status"],
                "decision": existing,
            }
        raise HTTPException(status_code=409, detail="Request cannot take a decision")
    if not store.is_in_progress(request_id):
        raise HTTPException(status_code=409, detail="Request is not in progress")
    calculation = None
    if recommendation.decision == DecisionKind.APPROVE:
        attempt = int(record.get("attempts", 0)) + 1
        stored_calc = store.attempts_calculation(request_id, attempt)
        calculation = check_calculation(recommendation.payout, stored_calc)
    extra_fields = recommendation.model_extra or {}
    rec_dict = {
        **extra_fields,
        "decision": recommendation.decision.value,
        "payout": recommendation.payout,
        "calculation": calculation.model_dump() if calculation else None,
        "section": recommendation.section,
        "reason": recommendation.reason,
        "decided_by": recommendation.decided_by,
        "decided_at": now(),
        "manual_version": "2026.2",
        "outcome": extra_fields.get("outcome", recommendation.decision.value),
        "rationale": extra_fields.get("rationale", recommendation.reason),
        "policy_citations": extra_fields.get(
            "policy_citations", [recommendation.section]
        ),
        "refund_amount": recommendation.payout,
        "next_action": extra_fields.get("next_action", "Support agent review"),
        "priority_human_review": extra_fields.get("priority_human_review", False),
    }
    if not store.save_recommendation(request_id, rec_dict):
        raise HTTPException(status_code=409, detail="Request cannot take a decision")
    return {
        "request_id": request_id,
        "status": "waiting_for_agent_review",
        "decision": rec_dict,
    }


@app.post(
    "/api/requests/{request_id}/failure",
    operation_id="record_failure",
    dependencies=[Depends(require_agent_key)],
)
def record_failure(request_id: str, request: Request) -> dict[str, str]:
    """Count a failed attempt and return the request to the queue or to support."""
    store = store_of(request)
    require_request(store, request_id)
    result = store.fail_request(request_id)
    if result is None:
        raise HTTPException(status_code=409, detail="Request is not in progress")
    return {"request_id": request_id, "status": result}


@app.post(
    "/api/requests/{request_id}/review",
    operation_id="review_request",
    dependencies=[Depends(require_support_handler)],
)
def review_request(
    request_id: str,
    review: ReviewInput,
    request: Request,
) -> dict[str, Any]:
    """Record a support decision and ask the agent to finish any paused thread."""
    store = store_of(request)
    result = require_request(store, request_id)
    if result["status"] not in {"waiting_for_agent_review", "decided"}:
        raise HTTPException(status_code=409, detail="Request is not ready for review")
    saved_review = {
        "outcome": review.outcome,
        "notes": review.notes,
        "reviewed_by": review.reviewed_by,
        "reviewed_at": (result.get("review") or {}).get("reviewed_at")
        or datetime.now(UTC).isoformat(),
    }
    status = store.record_review(request_id, saved_review)
    if status is None:
        raise HTTPException(status_code=409, detail="Request was already reviewed")
    if result.get("recommendation") is not None:
        notify_agent_resume(request_id)
    updated = require_request(store, request_id)
    return {**updated, "request_id": request_id}


def _check_send_back_eligible(record: dict[str, Any]) -> None:
    """Refuse send-back unless the request is waiting with a non-security decision."""
    req_inner = record.get("request") or {}
    if any(
        src.get(key)
        for src in (record, req_inner)
        for key in ("copy_of", "evaluation_id")
    ):
        raise HTTPException(
            status_code=409, detail="Evaluation copies cannot be sent back"
        )
    if record["status"] != "waiting_for_agent_review":
        raise HTTPException(status_code=409, detail="Request is not waiting for review")
    rec = record.get("recommendation")
    if rec is None:
        raise HTTPException(
            status_code=409, detail="Request has no recommendation to send back"
        )
    citations = rec.get("policy_citations") or []
    if "Security check" in citations or rec.get("decided_by") == "Security check":
        raise HTTPException(
            status_code=409, detail="Security check holds cannot be sent back"
        )


@app.post(
    "/api/requests/{request_id}/send_back",
    operation_id="send_back_request",
    dependencies=[Depends(require_support_handler)],
)
def send_back_request(
    request_id: str,
    payload: SendBackInput,
    request: Request,
) -> dict[str, Any]:
    """Send a recommended request back to the agent with a required note."""
    store = store_of(request)
    record = require_request(store, request_id)
    _check_send_back_eligible(record)
    if not store.send_back_request(request_id, payload.note, payload.sent_back_by):
        raise HTTPException(status_code=409, detail="Request is not waiting for review")
    notify_agent_resume(request_id)
    updated = require_request(store, request_id)
    return {**updated, "request_id": request_id}


@app.post(
    "/api/requests/{request_id}/take_sent_back",
    operation_id="take_sent_back",
    dependencies=[Depends(require_agent_key)],
)
def take_sent_back(
    request_id: str, payload: TakeSentBackInput, request: Request
) -> dict[str, Any]:
    """Claim a sent-back request once for agent resume or queue work."""
    store = store_of(request)
    require_request(store, request_id)
    attempt = store.take_sent_back(request_id, payload.owner)
    if attempt is None:
        raise HTTPException(status_code=409, detail="Request is not sent back")
    return {"request_id": request_id, "status": "in_progress", "attempt": attempt}


@app.post(
    "/api/requests/{request_id}/heartbeat",
    operation_id="request_heartbeat",
    dependencies=[Depends(require_agent_key)],
)
def request_heartbeat(request_id: str, request: Request) -> dict[str, str]:
    """Record a live resume heartbeat for an in-progress request."""
    store = store_of(request)
    require_request(store, request_id)
    if not store.heartbeat_request(request_id):
        raise HTTPException(status_code=409, detail="Request is not in progress")
    return {"request_id": request_id, "status": "alive"}


@app.post(
    "/api/requests/{request_id}/complete",
    operation_id="complete_request",
    dependencies=[Depends(require_agent_key)],
)
def complete_request(
    request_id: str, completion: CompletionInput, request: Request
) -> dict[str, Any]:
    """Record the confirmed resolution and complete a reviewed support request."""
    store = store_of(request)
    require_request(store, request_id)
    payload = completion.model_dump()
    if not store.complete_request(request_id, payload):
        raise HTTPException(status_code=409, detail="Request cannot be completed")
    return {"request_id": request_id, "status": "decided", "review": payload}


@app.post(
    "/api/requests/{request_id}/hold",
    operation_id="hold_payment",
    dependencies=[Depends(require_agent_key)],
)
def hold_payment(request_id: str, hold: HoldInput, request: Request) -> dict[str, Any]:
    """Hold an approved request the Gateway refused to pay for support sign-off."""
    store = store_of(request)
    req = require_request(store, request_id)
    rec = req.get("recommendation")
    if not rec or rec.get("outcome") != "approve":
        raise HTTPException(status_code=409, detail="Request is not approved")
    store.hold_request(request_id, hold.reason)
    return require_request(store, request_id)


@app.post(
    "/api/runs",
    operation_id="start_run",
    dependencies=[Depends(require_support_handler)],
)
def start_run(
    run: RunInput, request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Start one agent run over the waiting requests, up to its cap."""
    store = store_of(request)
    run_id = store.create_run(run.cap)
    if run_id is None:
        raise HTTPException(status_code=409, detail="A run is already in progress")
    background_tasks.add_task(run_in_background, store, run_id, run.cap)
    return {"run_id": run_id, "cap": run.cap, "status": "running"}


@app.get(
    "/api/runs/current",
    operation_id="get_current_run",
    dependencies=[Depends(require_shared_read)],
)
def get_current_run(request: Request) -> dict[str, Any] | None:
    """Return the latest run with its to-do list."""
    return store_of(request).current_run()


@app.put(
    "/api/runs/{run_id}/todos",
    operation_id="write_run_todos",
    dependencies=[Depends(require_agent_key)],
)
def write_run_todos(run_id: str, todos: TodoList, request: Request) -> dict[str, str]:
    """Replace a running run's to-do list."""
    store = store_of(request)
    if not store.is_running(run_id):
        raise HTTPException(status_code=409, detail="Run is not running")
    store.write_todos(run_id, [todo.model_dump() for todo in todos.todos])
    return {"run_id": run_id, "status": "recorded"}


@app.post(
    "/api/runs/{run_id}/todos/{request_id}",
    operation_id="update_run_todo",
    dependencies=[Depends(require_agent_key)],
)
def update_run_todo(
    run_id: str, request_id: str, update: TodoStatusInput, request: Request
) -> dict[str, str]:
    """Set the status of one item on a running run's to-do list."""
    store = store_of(request)
    if not store.is_running(run_id):
        raise HTTPException(status_code=409, detail="Run is not running")
    store.update_todo(run_id, request_id, update.status)
    return {"run_id": run_id, "request_id": request_id, "status": update.status}


@app.post(
    "/api/runs/{run_id}/finish",
    operation_id="finish_run",
    dependencies=[Depends(require_agent_key)],
)
def finish_run(run_id: str, finish: RunFinishInput, request: Request) -> dict[str, str]:
    """Close a running run with its status and the queue agent's summary."""
    store = store_of(request)
    if not store.is_running(run_id):
        raise HTTPException(status_code=409, detail="Run is not running")
    store.finish_run(run_id, finish.status, finish.summary)
    return {"run_id": run_id, "status": finish.status}


handler = Mangum(app, lifespan="off")
