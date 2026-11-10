"""The Shoppie Agent's AgentCore Runtime entrypoint."""

import logging
import os
import random
import secrets
import threading
from collections.abc import Callable
from typing import Any, Literal

import httpx
from bedrock_agentcore.identity.auth import requires_api_key
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException

from agent import main, queue

logger = logging.getLogger(__name__)

CREDENTIAL_PREFIX = "AGENTCORE_CREDENTIAL_"
CREDENTIAL_SUFFIX = "_NAME"

app = BedrockAgentCoreApp()
_READY_LOCK = threading.Lock()
_READY_STATE = {"ready": False}


class HealthInput(BaseModel):
    """Health check payload."""

    model_config = ConfigDict(extra="forbid")
    operation: Literal["health"]


class WorkRunInput(BaseModel):
    """Run queue payload."""

    model_config = ConfigDict(extra="forbid")
    operation: Literal["work_run"]
    run_id: str = Field(min_length=1)


class ResumeRequestInput(BaseModel):
    """Resume support request payload."""

    model_config = ConfigDict(extra="forbid")
    operation: Literal["resume_request"]
    request_id: str = Field(min_length=1)


PayloadModel = HealthInput | WorkRunInput | ResumeRequestInput

PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "health": HealthInput,
    "work_run": WorkRunInput,
    "resume_request": ResumeRequestInput,
}


def credential_providers() -> dict[str, str]:
    """Return env-var to Identity provider names when running on Runtime."""
    providers: dict[str, str] = {}
    for key, value in os.environ.items():
        if (
            key.startswith(CREDENTIAL_PREFIX)
            and key.endswith(CREDENTIAL_SUFFIX)
            and value.strip()
        ):
            target = key[len(CREDENTIAL_PREFIX) : -len(CREDENTIAL_SUFFIX)]
            if target:
                providers[target] = value.strip()
    return providers


def on_runtime() -> bool:
    """Return True when AgentCore Runtime credential mappings are configured."""
    return bool(credential_providers())


def reset_ready() -> None:
    """Reset cached readiness state (for tests)."""
    with _READY_LOCK:
        _READY_STATE["ready"] = False


def _fetch_identity_secret(provider_name: str) -> str:
    """Read one credential from AgentCore Identity."""

    @requires_api_key(provider_name=provider_name)
    def _read(*, api_key: str = "") -> str:
        return api_key

    return _read()


def ensure_ready() -> None:
    """Initialize Runtime secrets, API key check, and checkpoint pool once."""
    if _READY_STATE["ready"]:
        return
    with _READY_LOCK:
        if _READY_STATE["ready"]:
            return
        providers = credential_providers()
        if providers:
            random.seed()
            for env_name, provider_name in providers.items():
                os.environ[env_name] = _fetch_identity_secret(provider_name)
        if not os.getenv("UNDERCOVER_API_KEY", "").strip():
            raise RuntimeError("UNDERCOVER_API_KEY is missing")
        if os.getenv("DATABASE_URL") and not os.getenv("SHOPPIE_DATABASE_PATH"):
            main.get_checkpointer()
        _READY_STATE["ready"] = True


def _authorization_header(context: RequestContext) -> str:
    """Extract the Authorization header from the AgentCore request context."""
    headers = context.request_headers or {}
    for name, value in headers.items():
        if name.lower() == "authorization":
            return value
    return ""


def _require_bearer_key(context: RequestContext) -> None:
    """Require Authorization: Bearer <UNDERCOVER_API_KEY> outside Runtime."""
    expected = os.getenv("UNDERCOVER_API_KEY", "").strip()
    if not expected:
        raise RuntimeError("UNDERCOVER_API_KEY is missing")
    scheme, _, token = _authorization_header(context).partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")


def _parse_payload(payload: dict[str, Any]) -> BaseModel:
    """Validate an operation payload strictly with no optional fields."""
    operation = payload.get("operation")
    if not isinstance(operation, str) or operation not in PAYLOAD_MODELS:
        raise HTTPException(
            status_code=400, detail=f"Unknown or missing operation: {operation!r}"
        )
    try:
        return PAYLOAD_MODELS[operation].model_validate(payload)
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _start_async_task(name: str, work: Callable[[], Any]) -> threading.Thread:
    """Track work with AgentCore ping status and run it on a background thread."""
    task_id = app.add_async_task(name)

    def _run() -> None:
        try:
            work()
        except Exception:
            logger.exception("Async task %s failed", name)
        finally:
            app.complete_async_task(task_id)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def _handle_resume(request_id: str) -> dict[str, Any]:
    """Validate and schedule resumption of a paused or sent-back support request."""
    try:
        prepared = main.prepare_resume(request_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        return {"refused": str(error)}
    _start_async_task(
        f"resume_request:{request_id}", lambda: main.execute_resume(prepared)
    )
    return {"accepted": True}


def _handle_work_run(run_id: str) -> dict[str, Any]:
    """Verify a run is still running and schedule queue work on a thread."""
    response = main.backend_get(f"/api/runs/{run_id}")
    if response.status_code == httpx.codes.NOT_FOUND:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    response.raise_for_status()
    run = response.json()
    if run.get("status") != "running":
        return {"refused": f"Run {run_id} is not running"}
    cap = int(run["cap"])
    _start_async_task(
        f"work_run:{run_id}",
        lambda: queue.work_run(run_id, cap),
    )
    return {"accepted": True}


def _dispatch(model: BaseModel) -> dict[str, Any]:
    """Execute a validated entrypoint operation."""
    if isinstance(model, HealthInput):
        return {"healthy": True}
    if isinstance(model, WorkRunInput):
        return _handle_work_run(model.run_id)
    if isinstance(model, ResumeRequestInput):
        return _handle_resume(model.request_id)
    raise HTTPException(status_code=400, detail="Unsupported payload")


@app.entrypoint
def invoke(payload: Any, context: RequestContext) -> dict[str, Any]:
    """Handle one AgentCore Runtime /invocations request."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    if not on_runtime() and payload.get("operation") != "health":
        _require_bearer_key(context)
    model = _parse_payload(payload)
    ensure_ready()
    return _dispatch(model)
