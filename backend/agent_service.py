"""Invoke the Shoppie Agent over HTTP (/invocations) or AgentCore Runtime."""

import json
import os
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
HTTP_TIMEOUT_SECONDS = 10.0
RUNTIME_CONNECT_TIMEOUT = 5
RUNTIME_READ_TIMEOUT = 20

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


def agent_bearer_headers() -> dict[str, str]:
    """Return the Authorization Bearer header for HTTP calls to the agent."""
    key = os.getenv("UNDERCOVER_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _classify_answer(data: Any) -> AgentCallResult:
    if isinstance(data, dict):
        if data.get("accepted") is True:
            return AgentCallResult("accepted")
        if "refused" in data:
            return AgentCallResult("refused", str(data["refused"]))
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


def _call_http(payload: dict[str, Any]) -> AgentCallResult:
    url = f"{agent_service_url()}/invocations"
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


def _call_runtime(runtime_arn: str, payload: dict[str, Any]) -> AgentCallResult:
    session_id = f"shoppie-{uuid4()}"
    try:
        response = _runtime_client().invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload),
        )
        raw_body = _read_runtime_body(response.get("response", b""))
    except (BotocoreConnectionError, ClientError) as error:
        return AgentCallResult("did_not_take", str(error))
    except (
        HTTPClientError,
        IncompleteReadError,
        ResponseStreamingError,
        UnicodeDecodeError,
        OSError,
    ) as error:
        return AgentCallResult("uncertain", f"timed out or cut off ({error})")
    except BotoCoreError as error:
        return AgentCallResult("did_not_take", str(error))
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
