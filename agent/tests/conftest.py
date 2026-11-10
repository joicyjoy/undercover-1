from contextlib import nullcontext

import pytest
from fastapi.testclient import TestClient

from agent import api as agent_api
from agent import entrypoint, main
from agent.tests.gateway_stand_in import GatewayStandIn
from agent.tests.local_server import serving
from agent.tests.stubs import screening_stub
from backend.main import app
from backend.store import Store


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    """Set fake AWS credentials and clear Runtime ARN/ready state across tests."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key-id")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("UNDERCOVER_AGENT_RUNTIME", raising=False)
    entrypoint.reset_ready()
    yield
    entrypoint.reset_ready()


@pytest.fixture
def api(tmp_path, monkeypatch):
    """Run the backend on a fresh database and point the agent at it."""
    monkeypatch.setenv("SHOPPIE_DATABASE_PATH", str(tmp_path / "agent.sqlite3"))
    monkeypatch.setenv("UNDERCOVER_API_KEY", "test-agent-secret-key-0123456789abcdef")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(main, "screening_model", screening_stub)
    with TestClient(app) as client:
        monkeypatch.setattr(main, "backend", lambda: nullcontext(client))
        monkeypatch.setattr(agent_api, "backend", client)

        orig_save = agent_api.save_recommendation

        def auto_save(request_id, recommendation=None, **extra):
            rec = {**(recommendation or {}), **extra}
            outcome = rec.get("outcome") or rec.get("decision")
            if outcome == "approve":
                store = app.state.store
                req = store.get_request(request_id)
                attempt = int(req.get("attempts", 0)) + 1 if req else 1
                calc = store.attempts_calculation(request_id, attempt)
                if calc is not None:
                    payout = calc.payout
                else:
                    payout = float(
                        rec.get("refund_amount", rec.get("payout", 300.0)) or 300.0
                    )
                    store.keep_calculation(
                        request_id,
                        attempt,
                        [{"step": "The covered costs", "amount": payout}],
                        payout,
                    )
                rec["refund_amount"] = payout
                rec["payout"] = payout
            return orig_save(request_id, rec)

        monkeypatch.setattr(agent_api, "save_recommendation", auto_save)
        yield client


@pytest.fixture
def store(api) -> Store:
    """Return the store behind the test backend."""
    return app.state.store


@pytest.fixture(scope="session")
def gateway_server():
    """The stand-in Gateway, served on a local port for the whole session."""
    stand_in = GatewayStandIn()
    with serving(stand_in.asgi()) as address:
        stand_in.url = f"{address}/mcp"
        yield stand_in


@pytest.fixture
def gateway(gateway_server):
    """The stand-in Gateway, with nothing called or refused yet in this test."""
    gateway_server.reset()
    return gateway_server
