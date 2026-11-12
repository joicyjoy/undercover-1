"""Invoke the Shoppie Agent over HTTP (/invocations) or AgentCore Runtime."""

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    HTTPClientError,
    IncompleteReadError,
    ResponseStreamingError,
)
from botocore.exceptions import ConnectionError as BotocoreConnectionError

DEFAULT_AGENT_URL = "http://127.0.0.1:8080"
DEFAULT_COPILOT_URL = "http://127.0.0.1:8081"
HTTP_TIMEOUT_SECONDS = 10.0
RUNTIME_CONNECT_TIMEOUT = 5
RUNTIME_READ_TIMEOUT = 20
TRANSIENT_CONFLICT_CODES = {
    "ConflictException",
    "ResourceConflictException",
    "ServiceUnavailableException",
}

CallState = Literal["accepted", "refused", "did_not_take", "uncertain"]


@dataclass(frozen=True)
class AgentCallResult:
    """Outcome of asking the agent service or Runtime to start work."""

    state: CallState
    reason: str = ""


def agent_service_url() -> str:
    """Return the local or private HTTP base URL of the Shoppie Agent."""
    return os.getenv("UNDERCOVER_AGENT", DEFAULT_AGENT_URL).rstrip("/")


def agent_runtime_arn() -> str | None:
    """Return UNDERCOVER_AGENT_RUNTIME when configured, else None."""
    arn = os.getenv("UNDERCOVER_AGENT_RUNTIME", "").strip()
    return arn or None


def copilot_service_url() -> str:
    """Return the local or private HTTP base URL of the Shoppie Copilot."""
    return os.getenv("UNDERCOVER_COPILOT", DEFAULT_COPILOT_URL).rstrip("/")


def copilot_runtime_arn() -> str | None:
    """Return UNDERCOVER_COPILOT_RUNTIME when configured, else None."""
    arn = os.getenv("UNDERCOVER_COPILOT_RUNTIME", "").strip()
    return arn or None


def copilot_runtime_session_id(conversation_id: str) -> str:
    """Build a valid AgentCore Runtime session id (min 33 chars) for a conversation."""
    safe = "".join(ch for ch in conversation_id if ch.isalnum() or ch in "-_")
    return f"shoppie-copilot-{safe or 'session'}".ljust(36, "0")


def agent_bearer_headers() -> dict[str, str]:
    """Return the Authorization Bearer header for HTTP calls to the agent."""
    key = os.getenv("UNDERCOVER_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _classify_answer(data: Any) -> AgentCallResult:
    if isinstance(data, dict):
        if data.get("accepted") is True or data.get("status") == "accepted":
            return AgentCallResult("accepted")
        if "refused" in data:
            return AgentCallResult("refused", str(data["refused"]))
        if data.get("status") == "rejected":
            reason = data.get("error") or data.get("reason") or "rejected"
            return AgentCallResult("refused", str(reason))
        if "error" in data:
            return AgentCallResult("did_not_take", str(data["error"]))
    return AgentCallResult("uncertain", f"unexpected agent response: {data!r}")


def _extract_http_error(response: httpx.Response) -> str:
    detail = response.text
    try:
        body = response.json()
    except ValueError:
        return detail
    if isinstance(body, dict):
        return str(body.get("error") or body.get("detail") or detail)
    return detail


def _call_http(payload: dict[str, Any], base_url: str | None = None) -> AgentCallResult:
    target = (base_url or agent_service_url()).rstrip("/")
    url = f"{target}/invocations"
    try:
        response = httpx.post(
            url,
            json=payload,
            headers=agent_bearer_headers(),
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
        return AgentCallResult("did_not_take", f"could not connect ({error})")
    except (httpx.TimeoutException, httpx.TransportError) as error:
        return AgentCallResult("uncertain", f"timed out or interrupted ({error})")
    if response.status_code >= httpx.codes.BAD_REQUEST:
        detail = _extract_http_error(response)
        return AgentCallResult("did_not_take", f"HTTP {response.status_code}: {detail}")
    try:
        data = response.json()
    except ValueError as error:
        return AgentCallResult("uncertain", f"cut-off response ({error})")
    return _classify_answer(data)


def _runtime_client() -> Any:
    return boto3.client(
        "bedrock-agentcore",
        config=Config(
            retries={"total_max_attempts": 1},
            connect_timeout=RUNTIME_CONNECT_TIMEOUT,
            read_timeout=RUNTIME_READ_TIMEOUT,
        ),
    )


def _read_runtime_body(body: Any) -> str:
    raw = body.read() if hasattr(body, "read") else body
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


def _is_transient_conflict(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code") or "")
    return code in TRANSIENT_CONFLICT_CODES


def _call_runtime(
    runtime_arn: str,
    payload: dict[str, Any],
    session_id: str | None = None,
    conflict_attempts: int = 1,
) -> AgentCallResult:
    active_session = session_id or f"shoppie-{uuid4()}"
    response: dict[str, Any] = {}
    raw_body = ""
    failure: AgentCallResult | None = None
    for attempt in range(max(1, conflict_attempts)):
        try:
            response = _runtime_client().invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                runtimeSessionId=active_session,
                contentType="application/json",
                accept="application/json",
                payload=json.dumps(payload),
            )
            raw_body = _read_runtime_body(response.get("response", b""))
            break
        except ClientError as error:
            if _is_transient_conflict(error) and attempt + 1 < conflict_attempts:
                time.sleep(0.25)
                active_session = f"shoppie-copilot-{uuid4()}"
                continue
            failure = AgentCallResult("did_not_take", str(error))
        except BotocoreConnectionError as error:
            failure = AgentCallResult("did_not_take", str(error))
        except (
            HTTPClientError,
            IncompleteReadError,
            ResponseStreamingError,
            UnicodeDecodeError,
            OSError,
        ) as error:
            failure = AgentCallResult("uncertain", f"timed out or cut off ({error})")
        except BotoCoreError as error:
            failure = AgentCallResult("did_not_take", str(error))
        break
    if failure is not None:
        return failure
    status_code = int(response.get("statusCode", 200))
    if status_code >= httpx.codes.BAD_REQUEST:
        return AgentCallResult("did_not_take", f"HTTP {status_code}: {raw_body}")
    try:
        data = json.loads(raw_body)
    except ValueError as error:
        return AgentCallResult("uncertain", f"cut-off response ({error})")
    return _classify_answer(data)


def call_agent(payload: dict[str, Any]) -> AgentCallResult:
    """Send one non-retried invocation over Runtime (when set) or HTTP."""
    runtime_arn = agent_runtime_arn()
    if runtime_arn:
        return _call_runtime(runtime_arn, payload)
    return _call_http(payload)


def call_copilot(payload: dict[str, Any], conversation_id: str = "") -> AgentCallResult:
    """Invoke the Copilot AgentCore Runtime or HTTP service with conflict retries."""
    runtime_arn = copilot_runtime_arn()
    if runtime_arn:
        session_id = (
            copilot_runtime_session_id(conversation_id)
            if conversation_id
            else f"shoppie-copilot-{uuid4()}"
        )
        return _call_runtime(
            runtime_arn,
            payload,
            session_id=session_id,
            conflict_attempts=3,
        )
    return _call_http(payload, base_url=copilot_service_url())
