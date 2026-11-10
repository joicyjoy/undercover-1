"""The Shoppie Agent web service for resuming signed-off requests and starting runs."""

import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from agent import main, queue


def require_agent_api_key() -> str:
    """Return UNDERCOVER_API_KEY or refuse to start when it is missing."""
    key = os.getenv("UNDERCOVER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("UNDERCOVER_API_KEY is missing")
    return key


def require_backend_key(request: Request) -> None:
    """Refuse calls that do not carry the shared X-API-Key."""
    supplied = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(supplied, require_agent_api_key()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Refuse to start the agent service when UNDERCOVER_API_KEY is missing."""
    require_agent_api_key()
    yield


app = FastAPI(title="Shoppie Agent", lifespan=lifespan)


class AgentRunInput(BaseModel):
    """The most requests a queue run may work."""

    cap: int = Field(ge=1)


@app.get("/health", operation_id="agent_health")
def health() -> dict[str, str]:
    """Check that the Shoppie Agent service is running."""
    return {"status": "ok"}


@app.post("/requests/{request_id}/resume", operation_id="resume_request")
def resume_request(
    request_id: str, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Resume a paused or sent-back support request thread."""
    try:
        prepared = main.prepare_resume(request_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    background_tasks.add_task(main.execute_resume, prepared)
    return {"request_id": request_id, "status": "accepted"}


@app.post("/runs", operation_id="start_agent_run")
def start_agent_run(run: AgentRunInput) -> dict[str, Any]:
    """Start a capped queue run through the Shoppie backend."""
    with main.backend() as client:
        response = client.post("/api/runs", json={"cap": run.cap})
    if response.status_code == httpx.codes.CONFLICT:
        raise HTTPException(status_code=409, detail="A run is already in progress")
    response.raise_for_status()
    return response.json()


@app.post(
    "/runs/{run_id}/work",
    operation_id="work_agent_run",
    dependencies=[Depends(require_backend_key)],
)
def work_agent_run(
    run_id: str, run: AgentRunInput, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Work a run the backend created and report back when it ends."""
    background_tasks.add_task(queue.work_run, run_id, run.cap)
    return {"run_id": run_id, "cap": run.cap, "status": "accepted"}
