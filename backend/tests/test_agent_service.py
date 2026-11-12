import io
import json
from typing import Any

from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    IncompleteReadError,
    ReadTimeoutError,
)

from backend import agent_service

TEST_KEY = "test-agent-secret-key-0123456789abcdef"


def test_http_route_outcomes(monkeypatch):
    monkeypatch.setenv("UNDERCOVER_API_KEY", TEST_KEY)
    monkeypatch.setenv("UNDERCOVER_AGENT", "http://agent.local:8080/")

    class FakeResponse:
        def __init__(self, status_code: int, body: bytes) -> None:
            self.status_code = status_code
            self._body = body

        @property
        def text(self) -> str:
            return self._body.decode("utf-8", errors="replace")

        def json(self) -> Any:
            return json.loads(self._body)

    sent: list[dict[str, Any]] = []

    def respond_ok(url: str, **kwargs: Any) -> FakeResponse:
        sent.append({"url": url, **kwargs})
        return FakeResponse(200, b'{"accepted": true}')

    monkeypatch.setattr(agent_service.httpx, "post", respond_ok)
    accepted = agent_service.call_agent({"operation": "work_run", "run_id": "RUN-1"})
    assert accepted == agent_service.AgentCallResult("accepted")
    assert sent[0]["url"] == "http://agent.local:8080/invocations"
    assert sent[0]["headers"] == {"Authorization": f"Bearer {TEST_KEY}"}
    assert sent[0]["timeout"] == 10.0

    monkeypatch.setattr(
        agent_service.httpx,
        "post",
        lambda url, **kw: FakeResponse(200, b'{"refused": "Already busy"}'),
    )
    assert agent_service.call_agent({"operation": "work_run"}) == (
        agent_service.AgentCallResult("refused", "Already busy")
    )

    monkeypatch.setattr(
        agent_service.httpx,
        "post",
        lambda url, **kw: FakeResponse(401, b'{"error": "Unauthorized"}'),
    )
    http_err = agent_service.call_agent({"operation": "work_run"})
    assert http_err.state == "did_not_take"
    assert "HTTP 401" in http_err.reason

    def raise_connect(url: str, **kw: Any) -> None:
        raise agent_service.httpx.ConnectError("refused")

    monkeypatch.setattr(agent_service.httpx, "post", raise_connect)
    assert agent_service.call_agent({"operation": "work_run"}).state == "did_not_take"

    def raise_timeout(url: str, **kw: Any) -> None:
        raise agent_service.httpx.ReadTimeout("slow")

    monkeypatch.setattr(agent_service.httpx, "post", raise_timeout)
    assert agent_service.call_agent({"operation": "work_run"}).state == "uncertain"

    monkeypatch.setattr(
        agent_service.httpx,
        "post",
        lambda url, **kw: FakeResponse(200, b'{"accepted": tr'),
    )
    assert agent_service.call_agent({"operation": "work_run"}).state == "uncertain"


def test_runtime_route_config_session_and_outcomes(monkeypatch):
    monkeypatch.setenv(
        "UNDERCOVER_AGENT_RUNTIME",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/shoppie",
    )
    monkeypatch.setenv("UNDERCOVER_API_KEY", TEST_KEY)

    configs: list[Any] = []
    invocations: list[dict[str, Any]] = []
    outcome_factory: Any = None

    class FakeClient:
        def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
            invocations.append(kwargs)
            return outcome_factory()

    def fake_boto3_client(service_name: str, **kwargs: Any) -> FakeClient:
        configs.append((service_name, kwargs["config"]))
        return FakeClient()

    monkeypatch.setattr(agent_service.boto3, "client", fake_boto3_client)

    outcome_factory = lambda: {  # noqa: E731
        "statusCode": 200,
        "response": io.BytesIO(b'{"accepted": true}'),
    }
    res = agent_service.call_agent({"operation": "resume_request", "request_id": "R1"})
    assert res == agent_service.AgentCallResult("accepted")
    assert configs[0][0] == "bedrock-agentcore"
    assert configs[0][1].retries == {"total_max_attempts": 1}
    assert configs[0][1].connect_timeout == 5
    assert configs[0][1].read_timeout == 20
    assert invocations[0]["agentRuntimeArn"].endswith("runtime/shoppie")
    assert len(invocations[0]["runtimeSessionId"]) >= 33
    assert json.loads(invocations[0]["payload"]) == {
        "operation": "resume_request",
        "request_id": "R1",
    }

    outcome_factory = lambda: {  # noqa: E731
        "statusCode": 200,
        "response": io.BytesIO(b'{"refused": "Not paused"}'),
    }
    assert agent_service.call_agent({"operation": "resume_request"}) == (
        agent_service.AgentCallResult("refused", "Not paused")
    )

    def fail_connect() -> dict[str, Any]:
        raise ConnectTimeoutError(endpoint_url="https://runtime")

    outcome_factory = fail_connect
    assert agent_service.call_agent({"operation": "work_run"}).state == "did_not_take"

    def fail_client() -> dict[str, Any]:
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Denied"}},
            "InvokeAgentRuntime",
        )

    outcome_factory = fail_client
    assert agent_service.call_agent({"operation": "work_run"}).state == "did_not_take"

    def fail_read() -> dict[str, Any]:
        raise ReadTimeoutError(endpoint_url="https://runtime")

    outcome_factory = fail_read
    assert agent_service.call_agent({"operation": "work_run"}).state == "uncertain"

    class CutStream:
        def read(self) -> bytes:
            raise IncompleteReadError(actual_bytes=4, expected_bytes=10)

    outcome_factory = lambda: {"statusCode": 200, "response": CutStream()}  # noqa: E731
    assert agent_service.call_agent({"operation": "work_run"}).state == "uncertain"

    outcome_factory = lambda: {  # noqa: E731
        "statusCode": 200,
        "response": io.BytesIO(b'{"accepted":'),
    }
    assert agent_service.call_agent({"operation": "work_run"}).state == "uncertain"


def test_call_copilot_service_url_and_runtime_conflict_recovery(monkeypatch):
    monkeypatch.delenv("UNDERCOVER_COPILOT", raising=False)
    monkeypatch.delenv("UNDERCOVER_COPILOT_RUNTIME", raising=False)
    assert agent_service.copilot_service_url() == "http://127.0.0.1:8081"
    monkeypatch.setenv("UNDERCOVER_COPILOT", "http://copilot.internal:8080/")
    assert agent_service.copilot_service_url() == "http://copilot.internal:8080"

    seen_urls: list[str] = []

    class FakeHttpResponse:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self) -> Any:
            return self._payload

    def fake_post(url: str, **kwargs: Any) -> FakeHttpResponse:
        seen_urls.append(url)
        if kwargs["json"].get("approved") is False:
            return FakeHttpResponse(200, {"status": "rejected", "reason": "Denied"})
        return FakeHttpResponse(200, {"status": "accepted"})

    monkeypatch.setenv("UNDERCOVER_API_KEY", TEST_KEY)
    monkeypatch.setattr(agent_service.httpx, "post", fake_post)
    assert agent_service.call_copilot(
        {"action": "copilot_turn"}, conversation_id="CONV-1"
    ) == agent_service.AgentCallResult("accepted")
    assert seen_urls[0] == "http://copilot.internal:8080/invocations"
    assert agent_service.call_copilot(
        {"action": "copilot_resume", "approved": False}, conversation_id="CONV-1"
    ) == agent_service.AgentCallResult("refused", "Denied")

    # Runtime path: ConflictException on first attempt recovers on retry
    session = agent_service.copilot_runtime_session_id("CONV-123")
    assert session.startswith("shoppie-copilot-")
    assert len(session) >= 33
    monkeypatch.setenv(
        "UNDERCOVER_COPILOT_RUNTIME",
        "arn:aws:bedrock-agentcore:us-east-1:111:runtime/copilot",
    )
    sessions_tried: list[str] = []

    class RetryClient:
        def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
            sessions_tried.append(kwargs["runtimeSessionId"])
            if len(sessions_tried) == 1:
                raise ClientError(
                    {"Error": {"Code": "ConflictException", "Message": "Busy"}},
                    "InvokeAgentRuntime",
                )
            return {
                "statusCode": 200,
                "response": io.BytesIO(b'{"status": "accepted"}'),
            }

    monkeypatch.setattr(agent_service.boto3, "client", lambda *_a, **_k: RetryClient())
    monkeypatch.setattr(agent_service.time, "sleep", lambda _s: None)
    res = agent_service.call_copilot(
        {"action": "copilot_turn"}, conversation_id="CONV-123"
    )
    assert res == agent_service.AgentCallResult("accepted")
    assert len(sessions_tried) == 2
    assert sessions_tried[0] == session
    assert sessions_tried[1] != session
