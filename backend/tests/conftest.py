import pytest


@pytest.fixture(autouse=True)
def clear_runtime_env(monkeypatch):
    """Ensure backend tests never call AWS AgentCore Runtime from local env."""
    monkeypatch.delenv("UNDERCOVER_AGENT_RUNTIME", raising=False)
